#!/usr/bin/python

import sys
import urllib2, urllib
import os
from Queue import Queue
import uuid
import time
import argparse
import json

from lib.uploader import get_authentication_info, get_upload_token, upload_file_list, upload_done_file, upload_summary
from lib.sequence import Sequence
from lib.exif import is_image, verify_exif
from lib.exifedit import create_mapillary_description
import lib.io

'''
Script for uploading images taken with other cameras than
the Mapillary iOS or Android apps.

It runs in the following steps:
    - Skip images that are potential duplicates (Move to path/duplicates)
    - Group images into sequences based on gps and time
    - Interpolate compass angles for each sequence
    - Add Mapillary tags to the images
    - Upload the images

The following EXIF tags are required:
-GPSLongitude
-GPSLatitude
-(GPSDateStamp and GPSTimeStamp) or DateTimeOriginal or DateTimeDigitized or DateTime

NB: RUN geotag_from_gpx.py first for images with GPS in a separated GPX file (e.g. GoPro)

NB: DO NOT USE THIS SCRIPT ON IMAGE FILES FROM THE MAPILLARY APPS,
USE UPLOAD.PY INSTEAD.

(assumes Python 2.x, for Python 3.x you need to change some module names)
'''

MAPILLARY_UPLOAD_URL = "https://s3-eu-west-1.amazonaws.com/mapillary.uploads.manual.images"
NUMBER_THREADS = int(os.getenv('NUMBER_THREADS', '2'))
MOVE_FILES = True

def log_file(path):
    return os.path.join(path, 'UPLOAD_LOG.txt')

def write_log(lines, path):
    with open(log_file(path), 'wb') as f:
        f.write(lines)

def read_log(path):
    if os.path.exists(log_file(path)):
        with open(log_file(path), 'rb') as f:
            lines = f.read()
    else:
        return None
    return lines

def processing_log_file(path):
    return os.path.join(path, 'PROCESSING_LOG.json')

def read_processing_log(path):
    with open(processing_log_file(path), 'rb') as f:
        log = json.loads(f.read())
    return log

def write_processing_log(log, path):
    with open(processing_log_file(path), 'wb') as f:
        f.write(json.dumps(log, indent=4))
    return log

if __name__ == '__main__':
    '''
    Use from command line as: python upload_with_preprocessing.py path

    You need to set the environment variables
        MAPILLARY_USERNAME
        MAPILLARY_EMAIL
        MAPILLARY_PASSWORD
        MAPILLARY_PERMISSION_HASH
        MAPILLARY_SIGNATURE_HASH
    to your unique values.

    You also need upload.py in the same folder or in your PYTHONPATH since this
    script uses pieces of that.
    '''

    if sys.version_info >= (3, 0):
        raise IOError("Incompatible Python version. This script requires Python 2.x, you are using {0}.".format(sys.version_info[:2]))

    parser = argparse.ArgumentParser(description='Upload photos to Mapillary with preprocessing')
    parser.add_argument('path', help='path to your photos')
    parser.add_argument('--cutoff_distance', default=600, help='maximum gps distance in meters within a sequence')
    parser.add_argument('--cutoff_time', default=60, help='maximum time interval in seconds within a sequence')
    parser.add_argument('--orientation', help='specify orientation of the images', default=1)
    parser.add_argument('--remove_duplicates', help='perform duplicate removal', action='store_true')
    parser.add_argument('--rerun', help='rerun the preprocessing and uploading', action='store_true')
    parser.add_argument('--interpolate_directions', help='perform interploation of directions', action='store_true')
    parser.add_argument('--skip_upload', help='skip uploading to server', action='store_true')
    parser.add_argument('--duplicate_distance', help='max distance for two images to be considered duplicates in meters', default=0.1)
    parser.add_argument('--duplicate_angle', help='max angle for two images to be considered duplicates in degrees', default=5)
    parser.add_argument('--auto_done', help='don`t ask for confirmation after every sequence but submit all', action='store_true')
    args = parser.parse_args()

    path = args.path
    cutoff_distance = float(args.cutoff_distance)
    cutoff_time = float(args.cutoff_time)
    skip_upload = args.skip_upload
    interpolate_directions = args.interpolate_directions
    orientation = int(args.orientation)
    auto_done = args.auto_done

    # Distance/Angle threshold for duplicate removal
    # NOTE: This might lead to removal of panorama sequences
    min_duplicate_distance = float(args.duplicate_distance)
    min_duplicate_angle = float(args.duplicate_angle)

    # Fetch authetication info
    try:
        MAPILLARY_USERNAME = os.environ['MAPILLARY_USERNAME']
        MAPILLARY_EMAIL = os.environ['MAPILLARY_EMAIL']
        MAPILLARY_PASSWORD = os.environ['MAPILLARY_PASSWORD']
        MAPILLARY_PERMISSION_HASH = os.environ['MAPILLARY_PERMISSION_HASH']
        MAPILLARY_SIGNATURE_HASH = os.environ['MAPILLARY_SIGNATURE_HASH']
    except KeyError:
        print("You are missing one of the environment variables MAPILLARY_USERNAME, MAPILLARY_EMAIL, MAPILLARY_PASSWORD, MAPILLARY_PERMISSION_HASH or MAPILLARY_SIGNATURE_HASH. These are required.")
        sys.exit()
    upload_token = get_upload_token(MAPILLARY_EMAIL, MAPILLARY_PASSWORD)

    # Check whether the directory has been processed before
    logs = read_log(path)
    retry_upload = False
    if logs is not None:
        s = Sequence(path)
        total_failed = len([f for f in s.file_list if 'failed' in f])
        print("This folder has been processed before. See summary below: \n{}".format(logs))
        if total_failed:
            print ("There are {} failed images.".format(total_failed))
            proceed = raw_input("Retry uploading failed images? [y/n]: ")
            if proceed in ["y", "Y", "yes", "Yes"]:
                retry_upload = True
                print("Start uploading failed images ...")
            elif proceed in ["n", "N", "no", "No"]:
                retry_upload = False
                print("Aborted. No retry on failed uploads")
                sys.exit()
            else:
                print('Please answer y or n. Try again.')
        else:
            print("Aborted. All uploads were successful in your last upload section.")
            sys.exit()

    duplicate_groups = {}
    split_groups = {}
    missing_groups = []
    s3_bucket_list = []
    total_uploads = 0

    if (not retry_upload) and (not os.path.exists(processing_log_file(path))):
        if args.rerun:
            skip_folders = []
        else:
            skip_folders = ['success', 'duplicates']

        s = Sequence(path, skip_folders=skip_folders)

        if s.num_images == 0:
            print("No images in the folder or all images have been successfully uploaded to Mapillary.")
            sys.exit()

        # Remove duplicates in a sequence (e.g. in case of red lights and in traffic)
        if args.remove_duplicates:
            print("\n=== Removing potentially duplicate images ...")
            duplicate_groups = s.remove_duplicates(min_duplicate_distance, min_duplicate_angle)

        # Split sequence based on distance and time
        print("\n=== Spliting photos into sequences based on time and distance ...")
        s = Sequence(path, skip_folders=['duplicates'])
        split_groups = s.split(cutoff_distance=cutoff_distance, cutoff_time=cutoff_time)

    # Add Mapillary tags
    if not os.path.exists(processing_log_file(path)) or args.rerun:
        print("\n=== Adding Mapillary tags and uploading per sequence ...")
        sequence_list = {}
        for root, sub_folders, files in os.walk(path):
            if ('duplicates' not in root) and ('success' not in root):
                s = Sequence(root, skip_folders=['duplicates', 'success'], skip_subfolders=True)

                # interpolate compass direction if missing
                print("\n=== Interpolating direction per sequence ...")
                directions = s.interpolate_direction()

                # Add a sequence uuid per sub-folder
                if len(s.file_list) > 0:
                    sequence_uuid = uuid.uuid4()
                    print("  sequence uuid: {}".format(sequence_uuid))

                file_list = []
                for i, filename in enumerate(s.file_list):
                    if is_image(filename):
                        filepath = os.path.join(filename)

                        # Determine whether use interpolated direction or not
                        if interpolate_directions and len(s.file_list) > 1:
                            bearing = directions[filepath]
                        else:
                            bearing = None

                        if verify_exif(filepath):
                            if not retry_upload:
                                # skip creating new sequence id for failed images
                                create_mapillary_description(filepath,
                                                             MAPILLARY_USERNAME,
                                                             MAPILLARY_EMAIL,
                                                             upload_token,
                                                             sequence_uuid,
                                                             bearing,
                                                             orientation)
                            file_list.append(filepath)
                        else:
                            missing_groups.append(filepath)
                    else:
                        print "   Ignoring {0}".format(os.path.join(root, filename))
                    lib.io.progress(i, len(s.file_list), 'Adding Mapillary tags')
                count = len(file_list)
                if count > 0:
                    sequence_list[str(sequence_uuid)] = file_list
            else:
                print("      Skipping images in {}".format(root))

        write_processing_log(sequence_list, path)
    else:
        print('This folder has been processed. Resuming unfinished uploads ...')
        sequence_list = read_processing_log(path)

    # Uploading images in each subfolder as a sequence
    if not skip_upload:
        for sequence_uuid, file_list in sequence_list.iteritems():
            file_list = [str(f) for f in file_list if os.path.exists(f)]
            count = len(file_list)
            s3_bucket = MAPILLARY_USERNAME+"/"+str(sequence_uuid)+"/"
            s3_bucket_list.append(s3_bucket)
            if count and not skip_upload:
                # upload a sequence
                print 'Uploading sequence {} to {}'.format(str(sequence_uuid), s3_bucket)

                # set upload parameters
                params = {"url": MAPILLARY_UPLOAD_URL,
                          "key": s3_bucket,
                          "permission": MAPILLARY_PERMISSION_HASH,
                          "signature": MAPILLARY_SIGNATURE_HASH,
                          "move_files": MOVE_FILES,
                          "keep_file_names": False}

                # Upload images
                total_uploads += len(file_list)
                upload_file_list(file_list, params)

    # A short summary of the uploads
    s = Sequence(path)
    lines = upload_summary(file_list, total_uploads, split_groups, duplicate_groups, missing_groups)
    print('\n========= Summary of your uploads ==============')
    print lines
    print("==================================================")

    print("You can now preview your uploads at http://www.mapillary.com/map/upload/im")

    # Finalizing the upload by uploading done files for all sequence
    if not skip_upload:
        print("\nFinalizing upload will submit all successful uploads and ignore all failed and duplicates.")
        print("If all files were marked as successful, everything is fine, just press 'y'.")

        # ask 3 times if input is unclear
        for i in range(3):
            proceed = "y"
            if not auto_done:
                proceed = raw_input("Finalize upload? [y/n]: ")
            if proceed in ["y", "Y", "yes", "Yes"]:
                for s3_bucket in s3_bucket_list:
                    # upload an empty DONE file for each sequence
                    params = {"url": MAPILLARY_UPLOAD_URL,
                              "key": s3_bucket,
                              "permission": MAPILLARY_PERMISSION_HASH,
                              "signature": MAPILLARY_SIGNATURE_HASH,
                              "move_files": False}
                    upload_done_file(params)
                    print("Done uploading.")
                break
            elif proceed in ["n", "N", "no", "No"]:
                print("Aborted. No files were submitted. Try again if you had failures.")
                break
            else:
                if i==2:
                    print("Aborted. No files were submitted. Try again if you had failures.")
                else:
                    print('Please answer y or n. Try again.')

        # store the logs after finalizing
        write_log(lines, path)

