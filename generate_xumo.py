# -*- coding: utf-8 -*-
import requests
import json
import os
import gzip
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
import logging
import time
import re
import uuid # Needed for IFA placeholder
import sys

# --- Configuration ---
ANDROID_TV_ENDPOINT = "https://android-tv-mds.xumo.com/v2"
VALENCIA_API_ENDPOINT = "https://valencia-app-mds.xumo.com/v2"
GEO_ID = "us"

# --- List IDs ---
VALENCIA_LIST_ID = "10006" # Specified by user for Valencia/Web endpoint
ANDROID_TV_LIST_ID = "10032" # Original ID for Android TV endpoint (used for fallback & EPG)

# --- Endpoint URLs ---
# Primary Attempt: Valencia Proxy using List ID 10006
PROXY_CHANNEL_LIST_URL = f"{VALENCIA_API_ENDPOINT}/proxy/channels/list/{VALENCIA_LIST_ID}.json?geoId={GEO_ID}"

# Fallback: Android TV using List ID 10032
ANDROID_TV_CHANNEL_LIST_URL = f"{ANDROID_TV_ENDPOINT}/channels/list/{ANDROID_TV_LIST_ID}.json?f=genreId&sort=hybrid&geoId={GEO_ID}"
# Fallback Stream Fetching (needs asset lookup)
BROADCAST_NOW_URL_TEMPLATE = f"{ANDROID_TV_ENDPOINT}/channels/channel/{{channel_id}}/broadcast.json?hour={{hour_num}}"
ASSET_DETAILS_URL_TEMPLATE = f"{ANDROID_TV_ENDPOINT}/assets/asset/{{asset_id}}.json?f=providers"
# EPG Fetching (using Android TV endpoint and its list ID)
EPG_FETCH_URL_TEMPLATE = f"{ANDROID_TV_ENDPOINT}/epg/{ANDROID_TV_LIST_ID}/{{date_str}}/0.json?limit=50&offset={{offset}}&f=asset.title&f=asset.descriptions"

# Generic Logo URL
XUMO_LOGO_URL_TEMPLATE = "https://image.xumo.com/v1/channels/channel/{channel_id}/168x168.png?type=color_onBlack"

# --- Script Settings ---
EPG_FETCH_DAYS = 2
MAX_EPG_OFFSET = 400 # Increased offset limit slightly
API_DELAY_SECONDS = 0.15
OUTPUT_DIR = "playlists"
PLAYLIST_FILENAME = "xumo_playlist.m3u"
EPG_FILENAME = "xumo_epg.xml.gz"
REQUEST_TIMEOUT = 45

# !!! IMPORTANT: VERIFY / UPDATE THESE !!!
GITHUB_USER = "BuddyChewChew"
GITHUB_REPO = "xumo-playlist-generator"
GITHUB_BRANCH = "main"
EPG_RAW_URL = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{OUTPUT_DIR}/{EPG_FILENAME}"

# --- Headers ---
WEB_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://play.xumo.com',
    'Referer': 'https://play.xumo.com/',
}
ANDROID_TV_HEADERS = {
    'User-Agent': 'okhttp/4.9.3',
}

# --- Logging Setup ---
# Use DEBUG for detailed output, especially for diagnosing EPG issues
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s', stream=sys.stdout)
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s', stream=sys.stdout)


# --- Helper Functions ---

def fetch_data(url, params=None, is_json=True, retries=2, delay=2, headers=WEB_HEADERS): # Default to WEB_HEADERS
    """Fetches data from a URL, handles JSON parsing and errors, includes retries."""
    logging.debug(f"URL: {url}, Params: {params}")
    logging.debug(f"Headers: {json.dumps(headers)}")
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            logging.debug(f"Request URL: {response.url}")
            logging.debug(f"Response Status: {response.status_code}")
            if logging.getLogger().level == logging.DEBUG: logging.debug(f"Response Headers: {json.dumps(dict(response.headers))}")

            response.raise_for_status()

            if is_json:
                if not response.content:
                     logging.warning(f"Empty response content received from {url}")
                     return None
                try:
                    parsed_json = response.json()
                    # Avoid overly verbose logging of full JSON unless absolutely needed
                    # if logging.getLogger().level == logging.DEBUG:
                    #      logging.debug(f"Raw JSON Response:\n{json.dumps(parsed_json, indent=2)}")
                    return parsed_json
                except json.JSONDecodeError as e_final:
                    logging.error(f"Error decoding JSON. Content: {response.text[:500]}... - {e_final}")
                    if logging.getLogger().level == logging.DEBUG: logging.debug(f"Full Text:\n{response.text}")
                    return None
            else:
                 try:
                     decoded_text = response.content.decode('utf-8', errors='ignore')
                     if logging.getLogger().level == logging.DEBUG: logging.debug(f"Raw Text Response:\n{decoded_text[:1500]}...")
                     return decoded_text
                 except Exception as decode_ex:
                     logging.error(f"Error decoding text response: {decode_ex}")
                     return None

        except requests.exceptions.HTTPError as e:
            logging.warning(f"Attempt {attempt+1}/{retries+1} HTTP Error: {e}")
            if response is not None: logging.warning(f"Error Response Content: {response.text[:500]}...")
            if attempt < retries and response is not None and response.status_code not in [401, 403, 404, 429]: # Added 429 (Too Many Requests)
                time.sleep(delay * (attempt + 1)) # Exponential backoff might be better
            elif attempt == retries:
                logging.error(f"Final attempt failed with HTTP Error: {e}")
                return None
            else: break # Non-retriable HTTP error or final attempt failed

        except requests.exceptions.RequestException as e:
            logging.warning(f"Attempt {attempt+1}/{retries+1} Network Error: {e}")
            if attempt < retries: time.sleep(delay * (attempt + 1))
            elif attempt == retries:
                logging.error(f"Final attempt failed with Network Error: {e}")
                return None
    return None

def format_xmltv_time(dt_obj):
    if not isinstance(dt_obj, datetime):
        logging.warning(f"format_xmltv_time received non-datetime object: {type(dt_obj)}")
        return ""
    if not dt_obj.tzinfo: dt_obj = dt_obj.replace(tzinfo=timezone.utc) # Assume UTC if naive
    # Ensure timezone is formatted correctly (+HHMM)
    return dt_obj.strftime('%Y%m%d%H%M%S %z').replace(":", "")


def parse_iso_datetime(iso_time_str):
    if not iso_time_str:
        logging.debug("parse_iso_datetime received empty string.")
        return None
    try:
        # Normalize timezone representation
        iso_time_str = iso_time_str.replace('Z', '+00:00')
        has_offset = '+' in iso_time_str[10:] or '-' in iso_time_str[10:]

        # Handle milliseconds by truncating BEFORE adding default offset
        if '.' in iso_time_str:
            ts_parts = iso_time_str.split('.', 1) # Split only once
            base_time = ts_parts[0]
            zone_part = ""
            # Check for offset after the decimal part
            if len(ts_parts) > 1:
                remainder = ts_parts[1]
                if '+' in remainder: zone_part = '+' + remainder.split('+', 1)[1]
                elif '-' in remainder: zone_part = '-' + remainder.split('-', 1)[1]
            iso_time_str = base_time + zone_part # Reconstruct without milliseconds
            has_offset = bool(zone_part) # Update based on extracted zone

        # Add default UTC offset only if none was found/extracted
        if not has_offset:
            iso_time_str += '+00:00'

        # Ensure the offset has the right format (e.g., +00:00) if needed by fromisoformat
        # Python's fromisoformat is generally robust here
        dt_obj = datetime.fromisoformat(iso_time_str)
        # Standardize to UTC
        return dt_obj.astimezone(timezone.utc)
    except Exception as e_inner:
        logging.warning(f"Could not parse ISO timestamp '{iso_time_str}': {e_inner}")
        return None


def ensure_output_dir():
    if not os.path.exists(OUTPUT_DIR):
        logging.info(f"Creating output directory: {OUTPUT_DIR}")
        try:
            os.makedirs(OUTPUT_DIR)
        except OSError as e:
            logging.error(f"Failed to create directory {OUTPUT_DIR}: {e}")
            raise # Re-raise exception to stop the script if dir creation fails

def save_gzipped_xml(tree, filepath):
    try:
        xml_string = ET.tostring(tree.getroot(), encoding='UTF-8', xml_declaration=True)
        with gzip.open(filepath, 'wb') as f: f.write(xml_string)
        logging.info(f"Gzipped EPG XML file saved: {filepath}")
    except Exception as e: logging.error(f"Error writing gzipped EPG file {filepath}: {e}")

def save_m3u(content, filepath):
    try:
        with open(filepath, 'w', encoding='utf-8') as f: f.write(content)
        logging.info(f"M3U playlist file saved: {filepath}")
    except Exception as e: logging.error(f"Error writing M3U file {filepath}: {e}")

def process_stream_uri(uri):
    # (Keep previous processing logic, assuming web-like placeholders)
    if not uri: return None
    try:
        uri = uri.replace('[PLATFORM]', "web")
        uri = uri.replace('[APP_VERSION]', "1.0.0") # Placeholder
        uri = uri.replace('[timestamp]', str(int(time.time()*1000)))
        uri = uri.replace('[app_bundle]', "web.xumo.com")
        uri = uri.replace('[device_make]', "GitHubAction")
        uri = uri.replace('[device_model]', "PythonScript")
        uri = uri.replace('[content_language]', "en")
        uri = uri.replace('[IS_LAT]', "0")
        uri = uri.replace('[IFA]', str(uuid.uuid4()))
        uri = uri.replace('[SESSION_ID]', str(uuid.uuid4()))
        uri = uri.replace('[DEVICE_ID]', str(uuid.uuid4().hex))
        uri = re.sub(r'\[([^]]+)\]', '', uri) # Remove leftovers
        return uri
    except Exception as e:
        logging.error(f"Error processing stream URI '{uri[:50]}...': {e}")
        return None


# --- Core Logic Functions ---

# <<< Primary Attempt: Use Valencia Proxy List Endpoint >>>
def get_channels_via_proxy_list():
    """Attempts to fetch channel list via the Valencia proxy LIST endpoint (10006)."""
    logging.info(f"Attempting Valencia Proxy List: {PROXY_CHANNEL_LIST_URL}")
    data = fetch_data(PROXY_CHANNEL_LIST_URL, is_json=True, retries=1, headers=WEB_HEADERS)

    if not data or not isinstance(data, dict):
        logging.warning(f"Failed to fetch valid dictionary data from Valencia proxy list endpoint.")
        return None

    processed_channels = []
    channel_items = []

    # Adapt based on actual JSON response structure seen in DEBUG logs
    if 'channel' in data and isinstance(data['channel'], dict) and 'item' in data['channel'] and isinstance(data['channel']['item'], list):
        channel_items = data['channel']['item']
        logging.debug("Found channel list under data['channel']['item']")
    elif 'items' in data and isinstance(data['items'], list):
        channel_items = data['items']
        logging.debug("Found channel list under data['items']")
    # Add other checks if necessary based on logs
    else:
        logging.error(f"Could not find channel list in Valencia proxy list response. Top-level keys: {list(data.keys())}")
        return None

    logging.info(f"Found {len(channel_items)} potential channel items in Valencia list response.")
    if not channel_items:
        logging.warning("Valencia list response contained an empty channel list.")
        return None

    for item in channel_items:
        if not isinstance(item, dict):
            logging.warning(f"Skipping non-dictionary item in channel list: {item}")
            continue
        try:
            channel_id = item.get('guid', {}).get('value') or item.get('id')
            title = item.get('title') or item.get('name')
            number_str = item.get('number')
            callsign = item.get('callsign', '')
            logo_url = item.get('images', {}).get('logo') or item.get('logo')

            genre_list = item.get('genre')
            genre = 'General'
            if isinstance(genre_list, list) and len(genre_list) > 0 and isinstance(genre_list[0], dict):
                 genre = genre_list[0].get('value', 'General')
            elif isinstance(genre_list, str): genre = genre_list

            raw_stream_uri = None
            stream_info = item.get('stream') or item.get('streams') or item.get('playback') or item.get('providers')

            if isinstance(stream_info, dict):
                 raw_stream_uri = stream_info.get('hls') or stream_info.get('m3u8') or stream_info.get('live') or stream_info.get('url') or stream_info.get('uri')
            elif isinstance(stream_info, list) and len(stream_info) > 0:
                 for provider in stream_info:
                     if isinstance(provider, dict) and 'sources' in provider and isinstance(provider['sources'], list):
                         for source in provider['sources']:
                             if isinstance(source, dict) and source.get('uri') and (source.get('type') == 'application/x-mpegURL' or source.get('uri','').endswith('.m3u8')):
                                 raw_stream_uri = source['uri']
                                 break
                         if raw_stream_uri: break

            properties = item.get('properties', {})
            is_live = properties.get('is_live') == "true"
            # Use callsign check only if properties['drm'] is not present? Or prioritize properties?
            # is_drm = properties.get('is_drm') == 'true' # Example: Check property first
            # if not is_drm: # If property doesn't say DRM, check callsign
            is_drm = callsign.endswith("-DRM") or callsign.endswith("DRM-CMS")

            if is_drm:
                 logging.debug(f"Skipping potential DRM channel: {channel_id} ({title})")
                 continue
            if not is_live:
                 logging.debug(f"Skipping non-live channel: {channel_id} ({title})")
                 continue

            if not channel_id or not title:
                logging.warning(f"Skipping item due to missing ID or title: {item}")
                continue

            channel_id_str = str(channel_id)

            final_logo_url = None
            if logo_url:
                 if logo_url.startswith('//'): final_logo_url = 'https:' + logo_url
                 elif logo_url.startswith('/'): final_logo_url = 'https://image.xumo.com' + logo_url
                 else: final_logo_url = logo_url
            else: final_logo_url = XUMO_LOGO_URL_TEMPLATE.replace("{channel_id}", channel_id_str)

            processed_stream_url = None
            if raw_stream_uri:
                processed_stream_url = process_stream_uri(raw_stream_uri)
                if not processed_stream_url:
                     logging.warning(f"Found raw stream URI for '{title}' ({channel_id_str}) but failed to process it: {raw_stream_uri[:100]}...")
            else:
                logging.debug(f"No direct stream URI found for channel '{title}' ({channel_id_str}) in Valencia list item.")

            processed_channels.append({
                'id': channel_id_str,
                'name': title,
                'number': str(number_str) if number_str else None,
                'callsign': callsign,
                'logo': final_logo_url,
                'group': genre,
                'stream_url': processed_stream_url,
            })

        except Exception as e:
             logging.warning(f"Error processing Valencia list item {item.get('id', 'N/A')}: {e}", exc_info=True)

    if not processed_channels:
        logging.warning("Valencia list endpoint returned data, but no channels could be successfully processed.")
        return None
    logging.info(f"Successfully processed {len(processed_channels)} channels from Valencia list endpoint.")
    return processed_channels


# --- Fallback Android TV Fetch Functions ---

def get_live_channels_list_android_tv():
    """Fetches master list from Android TV endpoint (10032)."""
    logging.info(f"Fetching Android TV Fallback List: {ANDROID_TV_CHANNEL_LIST_URL}")
    data = fetch_data(ANDROID_TV_CHANNEL_LIST_URL, is_json=True, headers=ANDROID_TV_HEADERS)

    if not data or 'channel' not in data or 'item' not in data['channel']:
        logging.error("Invalid or empty list response from Android TV endpoint.")
        return []

    live_channels = []
    for item in data['channel'].get('item', []):
        try:
            channel_id = item.get('guid', {}).get('value')
            title = item.get('title')
            callsign = item.get('callsign', '')
            properties = item.get('properties', {})
            is_live = properties.get('is_live') == "true"
            number_str = item.get('number')
            genre_list = item.get('genre')
            genre = 'General'
            if isinstance(genre_list, list) and len(genre_list) > 0 and isinstance(genre_list[0], dict):
                genre = genre_list[0].get('value', 'General')

            if callsign.endswith("-DRM") or callsign.endswith("DRM-CMS"):
                logging.debug(f"Skipping DRM channel: {channel_id} ({title})")
                continue
            if not is_live:
                logging.debug(f"Skipping non-live channel: {channel_id} ({title})")
                continue

            if channel_id and title:
                channel_id_str = str(channel_id)
                logo_url = XUMO_LOGO_URL_TEMPLATE.replace("{channel_id}", channel_id_str)
                live_channels.append({
                    'id': channel_id_str,
                    'name': title,
                    'number': number_str,
                    'callsign': callsign,
                    'logo': logo_url,
                    'group': genre,
                    'stream_url': None # Needs fetching separately
                })
            else:
                 logging.warning(f"Skipping Android channel item due to missing ID or title: {item}")
        except Exception as e:
             logging.warning(f"Error processing Android channel item {item}: {e}", exc_info=True)

    logging.info(f"Found {len(live_channels)} live, non-DRM channels from Android TV fallback list.")
    return live_channels


def fetch_stream_urls_via_asset_lookup(channels_list):
    """Fetches stream URLs via Android TV asset lookup method for channels missing them."""
    logging.info(f"Attempting Android TV asset lookup for {len(channels_list)} channels...")
    processed_count = 0
    channels_with_streams = [] # Build a new list with results

    for i, channel_info in enumerate(channels_list):
        channel_id = channel_info['id']

        if channel_info.get('stream_url'):
            logging.debug(f"Stream URL already present for {channel_id}, skipping asset lookup.")
            channels_with_streams.append(channel_info) # Keep the channel
            continue

        logging.debug(f"Asset Lookup: Processing {channel_id} ({channel_info['name']}) ({i+1}/{len(channels_list)})")

        current_hour = datetime.now(timezone.utc).hour
        broadcast_url = BROADCAST_NOW_URL_TEMPLATE.format(channel_id=channel_id, hour_num=current_hour)
        logging.debug(f"Fetching broadcast info: {broadcast_url}")
        broadcast_data = fetch_data(broadcast_url, is_json=True, retries=1, headers=ANDROID_TV_HEADERS)

        asset_id = None
        if broadcast_data and 'assets' in broadcast_data and isinstance(broadcast_data['assets'], list) and len(broadcast_data['assets']) > 0:
            now_utc = datetime.now(timezone.utc)
            current_asset = None
            for asset in broadcast_data['assets']:
                start_time = parse_iso_datetime(asset.get('start'))
                end_time = parse_iso_datetime(asset.get('end'))
                if start_time and end_time and start_time <= now_utc < end_time:
                    current_asset = asset
                    break
            if not current_asset and broadcast_data['assets']:
                 current_asset = broadcast_data['assets'][0]

            if current_asset: asset_id = current_asset.get('id')

            if asset_id: logging.debug(f"Found current asset ID {asset_id} for channel {channel_id}")
            else: logging.warning(f"Relevant asset in broadcast data for channel {channel_id} has no ID.")
        else:
            logging.warning(f"Could not get valid broadcast data or assets for channel {channel_id} (Hour: {current_hour})")
            # Decide whether to keep the channel in the list without a stream
            # channels_with_streams.append(channel_info) # Option: Keep it
            time.sleep(API_DELAY_SECONDS)
            continue # Option: Skip it if broadcast fails

        if not asset_id:
             logging.warning(f"No asset ID found for channel {channel_id}, cannot get stream URL.")
             # channels_with_streams.append(channel_info) # Option: Keep it
             time.sleep(API_DELAY_SECONDS)
             continue # Option: Skip it

        asset_details_url = ASSET_DETAILS_URL_TEMPLATE.format(asset_id=asset_id)
        logging.debug(f"Fetching asset details: {asset_details_url}")
        asset_data = fetch_data(asset_details_url, is_json=True, headers=ANDROID_TV_HEADERS)

        raw_stream_uri = None
        if asset_data and 'providers' in asset_data and isinstance(asset_data['providers'], list):
            for provider in asset_data['providers']:
                 if ('sources' in provider and isinstance(provider['sources'], list)):
                     for source in provider['sources']:
                         if source.get('uri') and (source.get('type') == 'application/x-mpegURL' or source.get('uri', '').endswith('.m3u8')):
                             raw_stream_uri = source['uri']
                             break
                         elif source.get('uri') and not raw_stream_uri: raw_stream_uri = source['uri']
                     if raw_stream_uri: break
        else:
             logging.warning(f"Could not find providers/sources for asset {asset_id} (Channel {channel_id})")

        if not raw_stream_uri:
            logging.warning(f"No stream URI found in sources for asset {asset_id} (Channel {channel_id})")
            # channels_with_streams.append(channel_info) # Option: Keep it
            time.sleep(API_DELAY_SECONDS)
            continue # Option: Skip it

        processed_stream_url = process_stream_uri(raw_stream_uri)

        if processed_stream_url:
            channel_info['stream_url'] = processed_stream_url
            logging.debug(f"Successfully processed stream URL for channel {channel_id} via asset lookup")
            channels_with_streams.append(channel_info) # Add updated channel to list
            processed_count += 1
        else:
            logging.warning(f"Failed to process stream URI for asset {asset_id} (Channel {channel_id})")
            # channels_with_streams.append(channel_info) # Option: Keep it even if processing fails? Unlikely needed.

        time.sleep(API_DELAY_SECONDS)

    logging.info(f"Asset lookup method obtained/verified stream URLs for {processed_count} channels.")
    # Return only channels that now have a stream URL
    return [ch for ch in channels_with_streams if ch.get('stream_url')]


# --- EPG Functions (Still uses Android TV endpoint 10032) ---

def fetch_epg_data(channel_list):
    """Fetches EPG data using the Android TV EPG endpoint (10032)."""
    if not channel_list: return {}

    logging.info(f"Fetching EPG data for {len(channel_list)} channels (using Android TV EPG endpoint)...")
    consolidated_epg = {channel['id']: [] for channel in channel_list}
    assets_cache = {}
    channel_ids_in_list = {ch['id'] for ch in channel_list} # Use the final list passed in

    today = datetime.now(timezone.utc)
    dates_to_fetch = [today + timedelta(days=d) for d in range(EPG_FETCH_DAYS)]
    total_requests = 0
    total_programs_fetched = 0 # Counter for programs from API

    for date_obj in dates_to_fetch:
        date_str = date_obj.strftime('%Y%m%d')
        offset = 0
        while offset <= MAX_EPG_OFFSET:
            total_requests += 1
            logging.debug(f"Fetching EPG - Date: {date_str}, Offset: {offset}")
            fetch_url = EPG_FETCH_URL_TEMPLATE.format(date_str=date_str, offset=offset)
            page_data = fetch_data(fetch_url, is_json=True, headers=ANDROID_TV_HEADERS)

            if not page_data or 'channels' not in page_data or not isinstance(page_data['channels'], list):
                # If 'channels' key exists but is empty, it's a valid end, not necessarily an error
                if page_data and 'channels' in page_data and not page_data['channels']:
                     logging.debug(f"No more EPG channels found for date {date_str} at offset {offset}.")
                else:
                     logging.warning(f"Invalid or missing EPG data structure for date {date_str}, offset {offset}. Response keys: {list(page_data.keys()) if isinstance(page_data, dict) else 'Non-dict response'}")
                break # Stop fetching offsets for this date

            if len(page_data['channels']) == 0:
                logging.debug(f"Empty EPG channels list for date {date_str} at offset {offset}. Stopping for this date.")
                break


            if 'assets' in page_data and isinstance(page_data['assets'], dict):
                assets_cache.update(page_data['assets'])

            found_program_count_page = 0
            processed_channel_count_this_page = 0
            for channel_schedule_data in page_data['channels']:
                channel_id = str(channel_schedule_data.get('channelId'))
                if channel_id in channel_ids_in_list: # Check against the FINAL list of channels for M3U
                    processed_channel_count_this_page += 1
                    if channel_id not in consolidated_epg: consolidated_epg[channel_id] = []

                    for program_schedule in channel_schedule_data.get('schedule', []):
                        total_programs_fetched += 1 # Increment raw count
                        asset_id = program_schedule.get('assetId')
                        asset_details = assets_cache.get(asset_id)
                        if asset_details:
                            # Combine schedule info + asset details
                            program_info = {
                                'start': program_schedule.get('start'),
                                'end': program_schedule.get('end'),
                                'assetId': asset_id,
                                'title': asset_details.get('title', 'Unknown Program'),
                                'descriptions': asset_details.get('descriptions',{}),
                                'episodeTitle': asset_details.get('episodeTitle'),
                            }
                            # Basic validation before adding
                            if program_info['start'] and program_info['end']:
                                consolidated_epg[channel_id].append(program_info)
                                found_program_count_page +=1
                            else:
                                logging.warning(f"EPG: Program for asset {asset_id} on channel {channel_id} missing start/end time in schedule.")
                        else:
                            logging.warning(f"EPG: Asset details not found for assetId {asset_id} on channel {channel_id} (Date: {date_str}, Offset: {offset})")

            logging.debug(f"EPG - Date: {date_str}, Offset: {offset}: Processed {processed_channel_count_this_page} relevant channels, found {found_program_count_page} valid program entries on page.")
            offset += 50
            time.sleep(API_DELAY_SECONDS) # Delay between EPG page fetches

    logging.info(f"Finished fetching EPG data after {total_requests} requests. Found {total_programs_fetched} raw program entries.")
    # Log total programs stored per channel for debugging
    for ch_id, progs in consolidated_epg.items():
        logging.debug(f"  Channel {ch_id}: Stored {len(progs)} program entries.")
    return consolidated_epg


# --- Generate M3U and EPG XML ---

# <<< MODIFIED Function >>>
def generate_epg_xml(channel_list_with_streams, consolidated_epg_data):
    """Generates XMLTV file, ensuring only one display-name per channel."""
    logging.info("Generating EPG XML structure...")
    tv_element = ET.Element('tv', attrib={'generator-info-name': f'{GITHUB_USER}-{GITHUB_REPO}'})
    programme_count = 0
    channel_ids_in_list = {c['id'] for c in channel_list_with_streams} # Set for faster lookups

    # Add channel elements
    logging.debug("Adding channel elements to EPG XML...")
    for channel in channel_list_with_streams:
        chan_el = ET.SubElement(tv_element, 'channel', attrib={'id': channel['id']})
        # --- Only one display-name: the channel name ---
        ET.SubElement(chan_el, 'display-name').text = channel['name']
        # --- Channel number is NOT added as display-name ---
        if channel['logo']:
            ET.SubElement(chan_el, 'icon', attrib={'src': channel['logo']})
        logging.debug(f"  Added channel: ID={channel['id']}, Name={channel['name']}")

    # Add programme elements
    logging.debug("Adding programme elements to EPG XML...")
    logging.debug(f"Number of channels in consolidated_epg_data: {len(consolidated_epg_data)}")
    total_programs_in_data = sum(len(progs) for progs in consolidated_epg_data.values())
    logging.debug(f"Total program entries fetched before filtering/processing: {total_programs_in_data}")

    for channel_id, programs in consolidated_epg_data.items():
        # Ensure we only add programs for channels that are actually in the final M3U list
        if channel_id not in channel_ids_in_list:
             logging.debug(f"Skipping EPG programs for channel {channel_id} as it's not in the final M3U list.")
             continue

        program_processed_for_channel = 0 # Debug counter for this channel
        logging.debug(f"Processing programs for channel {channel_id}...")
        for program in programs:
            program_asset_id = program.get('assetId', 'N/A') # Get asset ID for logging
            program_title = program.get('title', 'N/A') # Get title for logging
            logging.debug(f"  Processing program: AssetID={program_asset_id}, Title='{program_title}'")
            try:
                start_time_str = program.get('start')
                end_time_str = program.get('end')
                logging.debug(f"    Raw Times: Start='{start_time_str}', End='{end_time_str}'")

                start_time = parse_iso_datetime(start_time_str)
                end_time = parse_iso_datetime(end_time_str)
                # Check if parsing succeeded before formatting
                if not start_time or not end_time:
                     logging.warning(f"    Skipping program due to failed time parsing: AssetID={program_asset_id}, Title='{program_title}'")
                     continue # Skip to next program if times didn't parse

                logging.debug(f"    Parsed Times (UTC): Start={start_time}, End={end_time}")

                title = program.get('title', 'Unknown Program')
                desc_obj = program.get('descriptions', {})
                desc = desc_obj.get('large') or desc_obj.get('medium') or desc_obj.get('small') or desc_obj.get('tiny')
                episode_title = program.get('episodeTitle')
                asset_id = program.get('assetId') # Keep for episode-num

                start_formatted = format_xmltv_time(start_time)
                stop_formatted = format_xmltv_time(end_time)
                logging.debug(f"    Formatted Times: Start='{start_formatted}', Stop='{stop_formatted}'")

                # Check if formatting succeeded
                if start_formatted and stop_formatted:
                    prog_el = ET.SubElement(tv_element, 'programme', attrib={'start': start_formatted,'stop': stop_formatted,'channel': channel_id})
                    ET.SubElement(prog_el, 'title', attrib={'lang': 'en'}).text = title
                    if desc: ET.SubElement(prog_el, 'desc', attrib={'lang': 'en'}).text = desc
                    if episode_title and episode_title != title: ET.SubElement(prog_el, 'sub-title', attrib={'lang': 'en'}).text = episode_title
                    if asset_id:
                        system_type = "dd_progid" if asset_id.startswith("EP") else "dd_assetid" # Use generic assetid otherwise
                        ET.SubElement(prog_el, 'episode-num', attrib={'system': system_type}).text = asset_id
                    programme_count += 1
                    program_processed_for_channel += 1
                    logging.debug(f"    Successfully added <programme> element for '{title}'")
                else:
                    logging.warning(f"    Skipping program due to invalid formatted time: AssetID={program_asset_id}, Title='{title}' (Channel: {channel_id})")

            except Exception as e:
                # Log full traceback for unexpected errors during program processing
                logging.exception(f"Error processing EPG program item {program_asset_id} for channel {channel_id}: {e}")
        # Log how many programs were actually added for this channel
        logging.debug(f"  Finished processing channel {channel_id}, added {program_processed_for_channel} programme elements.")

    logging.info(f"Generated XML with {len(channel_list_with_streams)} channels and {programme_count} programmes.") # This final count should be > 0 if EPG fetch worked
    if programme_count == 0 and total_programs_in_data > 0:
        logging.warning("EPG data was fetched, but no valid program entries could be added to the XML. Check time parsing/formatting issues in DEBUG logs.")
    return ET.ElementTree(tv_element)


def generate_m3u_playlist(channel_list_with_streams):
    """Generates M3U playlist string."""
    logging.info("Generating M3U playlist...")
    playlist_parts = [f'#EXTM3U url-tvg="{EPG_RAW_URL}"\n'] # Reference the EPG URL
    added_count = 0

    # Sort channels numerically if number exists, otherwise alphabetically by name
    def sort_key(channel):
        try:
            # Prioritize number, treat missing number as very high
            num = int(channel.get('number', '999999'))
            return (num, channel['name'].lower())
        except (ValueError, TypeError):
            # Fallback if number isn't integer-like
            return (999999, channel['name'].lower())

    sorted_channels = sorted(channel_list_with_streams, key=sort_key)

    for channel in sorted_channels:
        stream_url = channel.get('stream_url')
        channel_id = channel['id']
        # Clean display name and group title for M3U EXTINF line
        display_name = channel['name'].replace(',', ';') # Replace comma in the final display name part
        group_title = channel.get('group', 'General').replace(',', ';') # Use default group if missing
        # Clean name used in tvg-name attribute (replace quotes)
        tvg_name = channel['name'].replace('"', "'")
        logo = channel.get("logo", "") # Get logo URL

        if stream_url: # Ensure URL exists and is not empty
            # Construct EXTINF line
            extinf_line = f'#EXTINF:-1 tvg-id="{channel_id}" tvg-name="{tvg_name}" tvg-logo="{logo}" group-title="{group_title}",{display_name}\n'
            playlist_parts.append(extinf_line)
            # Add stream URL line
            playlist_parts.append(f'{stream_url}\n')
            added_count += 1
        else:
             # This should not happen if the list is filtered correctly before this function
             logging.error(f"Channel {channel_id} ('{channel['name']}') reached M3U generation without a stream URL!")

    logging.info(f"Added {added_count} channels with stream URLs to M3U playlist.")
    return "".join(playlist_parts)


# --- Main Execution ---
if __name__ == "__main__":
    logging.info(f"--- Starting Xumo Scraper (Valencia List ID: {VALENCIA_LIST_ID}, Fallback/EPG List ID: {ANDROID_TV_LIST_ID}) ---")
    try:
        ensure_output_dir() # Create output dir, will raise error if it fails
    except Exception as e:
        logging.error(f"Halting script because output directory could not be ensured: {e}")
        sys.exit(1) # Stop if we can't create the output dir


    channel_list_from_primary = None
    final_channel_list_with_streams = None

    # --- Strategy: Try Valencia List (10006) First ---
    logging.info(f"Step 1: Attempting to get channel metadata via Valencia List endpoint ({VALENCIA_LIST_ID})...")
    channel_list_from_primary = get_channels_via_proxy_list()

    if channel_list_from_primary:
        logging.info(f"Found {len(channel_list_from_primary)} channels from Valencia endpoint.")
        streams_found_primary = sum(1 for ch in channel_list_from_primary if ch.get('stream_url'))
        logging.info(f"{streams_found_primary} channels from Valencia list include a direct stream URL.")

        # Use asset lookup ONLY if some streams are missing from primary list
        if streams_found_primary < len(channel_list_from_primary):
            logging.warning(f"{len(channel_list_from_primary) - streams_found_primary} channels from Valencia list are missing stream URLs.")
            logging.info("Step 2: Attempting asset lookup for channels missing stream URLs...")
            # Pass the list from Valencia; asset lookup will only process those missing streams
            final_channel_list_with_streams = fetch_stream_urls_via_asset_lookup(channel_list_from_primary)
        else:
            logging.info("All channels from Valencia list have stream URLs. Skipping asset lookup fetch.")
            final_channel_list_with_streams = [ch for ch in channel_list_from_primary if ch.get('stream_url')] # Ensure list only has ones with streams

    # --- Fallback: If Valencia List Failed Entirely ---
    else:
        logging.warning("Valencia List endpoint failed. Initiating Full Fallback: Android TV Method...")
        logging.info("Step 1 (Fallback): Getting channel list via Android TV endpoint...")
        fallback_channel_list = get_live_channels_list_android_tv()
        if not fallback_channel_list:
            logging.error("Fallback Failed: Could not get master channel list. Aborting.")
            sys.exit(1)

        logging.info("Step 2 (Fallback): Fetching stream URLs via Android TV asset lookup...")
        # Pass the fallback list; asset lookup will process all of them
        final_channel_list_with_streams = fetch_stream_urls_via_asset_lookup(fallback_channel_list)

    # --- Process the final list ---
    if not final_channel_list_with_streams:
         logging.warning("No channels with stream URLs found after trying all methods. Generating empty files.")
         # Create empty M3U
         save_m3u(f'#EXTM3U url-tvg="{EPG_RAW_URL}"\n', os.path.join(OUTPUT_DIR, PLAYLIST_FILENAME))
         # Create empty EPG XML Tree and save it gzipped
         empty_root = ET.Element('tv')
         empty_tree = ET.ElementTree(empty_root)
         save_gzipped_xml(empty_tree, os.path.join(OUTPUT_DIR, EPG_FILENAME))
         logging.info("Generated empty playlist and EPG files.")
         sys.exit(0)

    # Final filter shouldn't be necessary if fetch_stream_urls_via_asset_lookup works correctly, but safe to keep
    final_channel_list_with_streams = [ch for ch in final_channel_list_with_streams if ch.get('stream_url')]
    logging.info(f"Proceeding with {len(final_channel_list_with_streams)} channels confirmed to have stream URLs.")

    if not final_channel_list_with_streams:
        logging.warning("Filtering removed all channels (no streams found). Generating empty files.")
        save_m3u(f'#EXTM3U url-tvg="{EPG_RAW_URL}"\n', os.path.join(OUTPUT_DIR, PLAYLIST_FILENAME))
        empty_root = ET.Element('tv')
        empty_tree = ET.ElementTree(empty_root)
        save_gzipped_xml(empty_tree, os.path.join(OUTPUT_DIR, EPG_FILENAME))
        logging.info("Generated empty playlist and EPG files.")
        sys.exit(0)


    # Step 3: Fetch EPG Data (using Android TV endpoint 10032)
    # Pass the final list of channels that have streams
    epg_data = fetch_epg_data(final_channel_list_with_streams)

    # Step 4: Generate EPG XML using the modified function
    epg_tree = generate_epg_xml(final_channel_list_with_streams, epg_data)

    # Step 5: Generate M3U Playlist
    m3u_content = generate_m3u_playlist(final_channel_list_with_streams)

    # Step 6: Save Files
    save_m3u(m3u_content, os.path.join(OUTPUT_DIR, PLAYLIST_FILENAME))
    save_gzipped_xml(epg_tree, os.path.join(OUTPUT_DIR, EPG_FILENAME))

    logging.info("--- Xumo Scraper Finished Successfully ---")
