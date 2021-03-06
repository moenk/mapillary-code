import json
import os
import sys
import pprint
import time
import requests
from tqdm import tqdm


def upload_image(session, filepath):
    filename = os.path.basename(filepath)
    fields = session['fields'].copy()
    fields['key'] = session['key_prefix'] + filename
    with open(filepath, 'rb') as f:
        r = requests.post(session['url'], data=fields,
                          files={'file': (filename, f)})
    r.raise_for_status()


def create_session(subdir, access_token, client_id):
    data = {
        'type': 'images/sequence'
    }
    headers = {
        "Authorization": "Bearer " + access_token,
        "Content-Type": "application/json"
    }
    r = requests.post('https://a.mapillary.com/v3/me/uploads?client_id=' +
                      client_id, data=json.dumps(data), headers=headers)
    session = r.json()
    # pprint(session)
    tqdm.write("Session open: " + session['key'])
    f = open(subdir + os.sep + "session.json", "w")
    f.write(json.dumps(session, sort_keys=True, indent=4))
    f.close()
    return session


def publish_session(session, access_token, client_id):
    key = session['key']
    headers = {
        "Authorization": "Bearer " + access_token
    }
    r = requests.put("https://a.mapillary.com/v3/me/uploads/" +
                     key + "/closed?client_id=" + client_id, headers=headers)
    tqdm.write("*** Session published: " + session['key'])
    tqdm.write(pprint.pformat(r))
    return


def delete_session(access_token, client_id):
    headers = {
        "Authorization": "Bearer " + access_token
    }
    r = requests.get(
        "https://a.mapillary.com/v3/me/uploads?client_id=" + client_id, headers=headers)
    sessions = r.json()
    for session in sessions:
        print("Delete Session:", session['key'])
        r = requests.delete("https://a.mapillary.com/v3/uploads/" +
                            session['key'] + "?client_id=" + client_id, headers=headers)
        r.raise_for_status()


def image_files(files):
    return [f for f in files if f.lower().endswith('.jpg')
            or f.lower().endswith('.jpeg')]


def upload_folder(folder_path, dry_run):
    if dry_run:
        print("*** DRY RUN, NOT ACTUALLY UPLOADING ANY IMAGERY, THE FOLLOWING IS SAMPLE OUTPUT")

    client_id = 'd0FVV29VMDR6SUVrcV94cTdabHBoZzoxZjc2MTE1Mzc1YjMxNzhi'
    print("   *** Read access token")
    with open("accesstoken3.conf", "r") as image_name:
        access_token = image_name.read()

    if not(os.path.isdir(folder_path)):
        print("No valid directory given for upload as parameter.")
        exit(1)

    # compute totals for progress bar
    total_images = 0
    total_image_dirs = 0
    for path, _, files in os.walk(folder_path):
        new_images = len(image_files(files))
        total_images += new_images
        if new_images > 0:
            total_image_dirs += 1

    # initialize progress bars
    total_pbar = tqdm(total=total_images)
    if total_image_dirs > 1:
        dirs_pbar = tqdm(total=total_image_dirs)
    else:
        dirs_pbar = None

    for path, _, files in os.walk(folder_path):
        if len(files) > 0:
            tqdm.write("   *** Uploading directory: " + path)
            if not dry_run:
                session = create_session(path, access_token, client_id)
            for image_name in image_files(files):
                absolute_filepath = path + os.sep + image_name
                if not dry_run:
                    upload_image(session, absolute_filepath)
                else:
                    time.sleep(1/total_images)
                total_pbar.update()
            if not dry_run:
                publish_session(session, access_token, client_id)
            else:
                time.sleep(1)
            if dirs_pbar:
                dirs_pbar.update()
    total_pbar.close()
    if dirs_pbar:
        dirs_pbar.close()


#
#   Main
#
if __name__ == "__main__":
    print ("*** Mapillary Tiny Uploader ***")
    ordnerpfad = sys.argv[1]
    if not(os.path.isdir(ordnerpfad)):
        print("No valid directory given as parameter.")
        exit(1)
    upload_folder(ordnerpfad, False)
