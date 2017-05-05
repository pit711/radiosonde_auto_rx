#!/usr/bin/env python
#
# Radiosonde Auto RX Tools
#
# 2017-04 Mark Jessop <vk5qi@rfhead.net>
#
# The following binaries will need to be built and copied to this directory:
# rs92/rs92gps
# scan/rs_detect
#
# The following other packages are needed:
# rtl-sdr (for the rtl_power and rtl_fm utilities)
# sox
#
# Instructions:
# Modify config parameters below as required. Take note of the APRS_USER and APRS_PASS values.
# Run with: python auto_rx.py
# A log file will be written to log/<timestamp>.log
#
#
# TODO:
# [ ] Better handling of errors from the decoder sub-process.
#       [x] Handle no lat/long better. [Decoder won't output data at all if CRC fails.]
#       [-] Option to filter by DOP data
# [x] Automatic downloading of ephemeris data, instead of almanac.
# [ ] Better peak signal detection. (Maybe convolve known spectral masks over power data?)
# [ ] Habitat upload. 
# [x] Move configuration parameters to a separate file.
#   [x] Allow use of custom object name instead of sonde ID.
# [x] Build file. 
# [x] RS41 support.
# [ ] Use FSK demod from codec2-dev ? 



import numpy as np
import sys
import argparse
import logging
import datetime
import time
import os
import signal
import Queue
import subprocess
import traceback
from aprs_utils import *
from threading import Thread
from StringIO import StringIO
from findpeaks import *
from config_reader import *
from gps_grabber import *


# Internet Push Globals
APRS_OUTPUT_ENABLED = False
HABITAT_OUTPUT_ENABLED = False

INTERNET_PUSH_RUNNING = True
internet_push_queue = Queue.Queue(1)



def run_rtl_power(start, stop, step, filename="log_power.csv",  dwell = 20):
    """ Run rtl_power, with a timeout"""
    # rtl_power -f 400400000:403500000:800 -i20 -1 log_power.csv
    rtl_power_cmd = "timeout %d rtl_power -f %d:%d:%d -i %d -1 %s" % (dwell+10, start, stop, step, dwell, filename)
    logging.info("Running frequency scan.")
    ret_code = os.system(rtl_power_cmd)
    if ret_code == 1:
        logging.critical("rtl_power call failed!")
        sys.exit(1)
    else:
        return True

def read_rtl_power(filename):
    """ Read in frequency samples from a single-shot log file produced by rtl_power """

    # Output buffers.
    freq = np.array([])
    power = np.array([])

    freq_step = 0


    # Open file.
    f = open(filename,'r')

    # rtl_power log files are csv's, with the first 6 fields in each line describing the time and frequency scan parameters
    # for the remaining fields, which contain the power samples. 

    for line in f:
        # Split line into fields.
        fields = line.split(',')

        if len(fields) < 6:
            logging.error("Invalid number of samples in input file - corrupt?")
            raise Exception("Invalid number of samples in input file - corrupt?")

        start_date = fields[0]
        start_time = fields[1]
        start_freq = float(fields[2])
        stop_freq = float(fields[3])
        freq_step = float(fields[4])
        n_samples = int(fields[5])

        freq_range = np.arange(start_freq,stop_freq,freq_step)
        samples = np.loadtxt(StringIO(",".join(fields[6:])),delimiter=',')

        # Add frequency range and samples to output buffers.
        freq = np.append(freq, freq_range)
        power = np.append(power, samples)

    f.close()
    return (freq, power, freq_step)


def quantize_freq(freq_list, quantize=5000):
    """ Quantise a list of frequencies to steps of <quantize> Hz """
    return np.round(freq_list/quantize)*quantize

def detect_sonde(frequency, ppm=0, gain=0):
    """ Receive some FM and attempt to detect the presence of a radiosonde. """
    rx_test_command = "timeout 10s rtl_fm -p %d -M fm -s 15k -f %d 2>/dev/null |" % (ppm, frequency) 
    rx_test_command += "sox -t raw -r 15k -e s -b 16 -c 1 - -r 48000 -t wav - highpass 20 2>/dev/null |"
    rx_test_command += "./rs_detect -z -t 8 2>/dev/null"

    logging.info("Attempting sonde detection on %.3f MHz" % (frequency/1e6))
    ret_code = os.system(rx_test_command)

    ret_code = ret_code >> 8

    if ret_code == 3:
        logging.info("Detected a RS41!")
        return "RS41"
    elif ret_code == 4:
        logging.info("Detected a RS92!")
        return "RS92"
    else:
        return None


def process_rs_line(line):
    """ Process a line of output from the rs92gps decoder, converting it to a dict """
    # Sample output:
    #   0      1        2        3            4         5         6      7   8     9  10
    # 106,M3553150,2017-04-30,05:44:40.460,-34.72471,138.69178,-263.83, 0.1,265.0,0.3,OK
    try:

        params = line.split(',')
        if len(params) < 11:
            logging.error("Not enough parameters: %s" % line)
            return None

        # Attempt to extract parameters.
        rs_frame = {}
        rs_frame['frame'] = int(params[0])
        rs_frame['id'] = str(params[1])
        rs_frame['date'] = str(params[2])
        rs_frame['time'] = str(params[3])
        rs_frame['lat'] = float(params[4])
        rs_frame['lon'] = float(params[5])
        rs_frame['alt'] = float(params[6])
        rs_frame['vel_h'] = float(params[7])
        rs_frame['heading'] = float(params[8])
        rs_frame['vel_v'] = float(params[9])
        rs_frame['crc'] =  str(params[10])
        rs_frame['temp'] = 0.0
        rs_frame['humidity'] = 0.0

        logging.info("TELEMETRY: %s,%d,%s,%.5f,%.5f,%.1f" % (rs_frame['id'], rs_frame['frame'],rs_frame['time'], rs_frame['lat'], rs_frame['lon'], rs_frame['alt']))

        return rs_frame

    except:
        logging.error("Could not parse string: %s" % line)
        traceback.print_exc()
        return None

def decode_rs92(frequency, ppm=0, rx_queue=None, almanac=None, ephemeris=None):
    """ Decode a RS92 sonde """

    # Before we get started, do we need to download GPS data?
    if ephemeris == None:
        # If no ephemeris data defined, attempt to download it.
        # get_ephemeris will either return the saved file name, or None.
        ephemeris = get_ephemeris(destination="ephemeris.dat")

    # If ephemeris is still None, then we failed to download the ephemeris data.
    # Try and grab the almanac data instead.
    if ephemeris == None:
        logging.error("Could not obtain ephemeris data, trying to download an almanac.")
        almanac = get_almanac(destination="almanac.txt")
        if almanac == None:
            # We probably don't have an internet connection. Bomb out, since we can't do much with the sonde telemetry without an almanac!
            logging.critical("Could not obtain GPS ephemeris or almanac data.")
            return False


    decode_cmd = "rtl_fm -p %d -M fm -s 12k -f %d 2>/dev/null |" % (ppm, frequency)
    decode_cmd += "sox -t raw -r 12k -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - lowpass 2500 highpass 20 2>/dev/null |"

    # Note: I've got the check-CRC option hardcoded in here as always on. 
    # I figure this is prudent if we're going to proceed to push this telemetry data onto a map.

    if ephemeris != None:
        decode_cmd += "./rs92mod --crc --csv -e %s" % ephemeris
    elif almanac != None:
        decode_cmd += "./rs92mod --crc --csv -a %s" % almanac

    rx_start_time = time.time()

    rx = subprocess.Popen(decode_cmd, shell=True, stdin=None, stdout=subprocess.PIPE, preexec_fn=os.setsid)

    while True:
        try:
            line = rx.stdout.readline()
            if (line != None) and (line != ""):
                data = process_rs_line(line)

                if data != None:
                    # Add in a few fields that don't come from the sonde telemetry.
                    data['freq'] = "%.3f MHz" % (frequency/1e6)
                    data['type'] = "RS92"

                    if rx_queue != None:
                        try:
                            rx_queue.put_nowait(data)
                        except:
                            pass
        except:
            traceback.print_exc()
            logging.error("Could not read from rxer stdout?")
            os.killpg(os.getpgid(rx.pid), signal.SIGTERM)
            return


def decode_rs41(frequency, ppm=0, rx_queue=None):
    """ Decode a RS41 sonde """
    decode_cmd = "rtl_fm -p %d -M fm -s 12k -f %d 2>/dev/null |" % (ppm, frequency)
    decode_cmd += "sox -t raw -r 12k -e s -b 16 -c 1 - -r 48000 -b 8 -t wav - lowpass 2600 2>/dev/null |"

    # Note: I've got the check-CRC option hardcoded in here as always on. 
    # I figure this is prudent if we're going to proceed to push this telemetry data onto a map.

    decode_cmd += "./rs41mod --crc --csv"

    rx_start_time = time.time()

    rx = subprocess.Popen(decode_cmd, shell=True, stdin=None, stdout=subprocess.PIPE, preexec_fn=os.setsid)

    while True:
        try:
            line = rx.stdout.readline()
            if (line != None) and (line != ""):
                data = process_rs_line(line)

                if data != None:
                    # Add in a few fields that don't come from the sonde telemetry.
                    data['freq'] = "%.3f MHz" % (frequency/1e6)
                    data['type'] = "RS41"

                    if rx_queue != None:
                        try:
                            rx_queue.put_nowait(data)
                        except:
                            pass
        except:
            traceback.print_exc()
            logging.error("Could not read from rxer stdout?")
            os.killpg(os.getpgid(rx.pid), signal.SIGTERM)
            return

def internet_push_thread(station_config):
    """ Push a frame of sonde data into various internet services (APRS-IS, Habitat) """
    global internet_push_queue, INTERNET_PUSH_RUNNING
    print("Started Internet Push thread.")
    while INTERNET_PUSH_RUNNING:                    
        try:
            data = internet_push_queue.get_nowait()
        except:
            continue

        # APRS Upload
        if station_config['enable_aprs']:
            # Produce aprs comment, based on user config.
            aprs_comment = station_config['aprs_custom_comment']
            aprs_comment = aprs_comment.replace("<freq>", data['freq'])
            aprs_comment = aprs_comment.replace("<id>", data['id'])
            aprs_comment = aprs_comment.replace("<vel_v>", "%.1fm/s" % data['vel_v'])
            aprs_comment = aprs_comment.replace("<type>", data['type'])

            # Push data to APRS.
            aprs_data = push_balloon_to_aprs(data,object_name=station_config['aprs_object_id'],aprs_comment=aprs_comment,aprsUser=station_config['aprs_user'], aprsPass=station_config['aprs_pass'])
            logging.debug("Data pushed to APRS-IS: %s" % aprs_data)

        # Habitat Upload
        if station_config['enable_habitat']:
            # TODO: Habitat upload.
            pass

        time.sleep(config['upload_rate'])

    print("Closing thread.")


if __name__ == "__main__":

    # Setup logging.
    logging.basicConfig(format='%(asctime)s %(levelname)s:%(message)s', filename=datetime.datetime.utcnow().strftime("log/%Y%m%d-%H%M%S.log"), level=logging.DEBUG)
    logging.getLogger().addHandler(logging.StreamHandler())

    # Command line arguments. 
    parser = argparse.ArgumentParser()
    parser.add_argument("-c" ,"--config", default="station.cfg", help="Receive Station Configuration File")
    parser.add_argument("-f", "--frequency", default=0.0, help="Sonde Frequency (MHz) (bypass scan step).")
    args = parser.parse_args()

    # Attempt to read in configuration file. Use default config if reading fails.
    config = read_auto_rx_config(args.config)

    # Pull some variables out of the config.
    SEARCH_ATTEMPTS = config['search_attempts']


    #
    # STEP 1: Search for a sonde.
    #

    # Search variables.
    sonde_freq = 0.0
    sonde_type = None

    while SEARCH_ATTEMPTS>0:
        # Scan Band
        run_rtl_power(config['min_freq']*1e6, config['max_freq']*1e6, config['search_step'])

        # Read in result
        try:
            (freq, power, step) = read_rtl_power('log_power.csv')
        except Exception as e:
            logging.debug("Failed to read log_power.csv. Attempting to run rtl_power again.")
            SEARCH_ATTEMPTS -= 1
            time.sleep(10)
            continue

        # Rough approximation of the noise floor of the received power spectrum.
        power_nf = np.mean(power)

        # Detect peaks.
        peak_indices = detect_peaks(power, mph=(power_nf+config['min_snr']), mpd=(config['min_distance']/step), show = False)

        if len(peak_indices) == 0:
            logging.info("No peaks found on this pass.")
            SEARCH_ATTEMPTS -= 1
            time.sleep(10)
            continue

        # Sort peaks by power.
        peak_powers = power[peak_indices]
        peak_freqs = freq[peak_indices]
        peak_frequencies = peak_freqs[np.argsort(peak_powers)][::-1]

        # Quantize to nearest x kHz
        peak_frequencies = quantize_freq(peak_frequencies, config['quantization'])
        logging.info("Peaks found at (MHz): %s" % str(peak_frequencies/1e6))

        # Run rs_detect on each peak frequency, to determine if there is a sonde there.
        for freq in peak_frequencies:
            detected = detect_sonde(freq, ppm=config['rtlsdr_ppm'])
            if detected != None:
                sonde_freq = freq
                sonde_type = detected
                break

        if sonde_type != None:
            # Found a sonde! Break out of the while loop and attempt to decode it.
            break
        else:
            # No sondes found :-( Wait and try again.
            SEARCH_ATTEMPTS -= 1
            logging.warning("Search attempt failed, %d attempts remaining. Waiting %d seconds." % (SEARCH_ATTEMPTS, config['search_delay']))
            time.sleep(config['search_delay'])

    if SEARCH_ATTEMPTS == 0:
        logging.error("No sondes detcted, exiting.")
        sys.exit(0)

    logging.info("Starting decoding of %s on %.3f MHz" % (sonde_type, sonde_freq/1e6))

    # Start a thread to push data to the web.
    t = Thread(target=internet_push_thread, kwargs={'station_config':config})
    t.start()

    # Start decoding the sonde!
    if sonde_type == "RS92":
        decode_rs92(sonde_freq, ppm=config['rtlsdr_ppm'], rx_queue=internet_push_queue)
    elif sonde_type == "RS41":
        decode_rs41(sonde_freq, ppm=config['rtlsdr_ppm'], rx_queue=internet_push_queue)
    else:
        pass

    # Stop the APRS output thread.
    INTERNET_PUSH_RUNNING = False

