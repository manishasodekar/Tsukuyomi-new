import fnmatch
import io
import logging
import traceback
from datetime import datetime
from typing import Optional
import av
import time
import json
import wave
import boto3
import requests
from io import BytesIO
from config.logconfig import get_logger
from botocore.exceptions import NoCredentialsError
from gevent import Timeout
from utils import heconstants

logger = get_logger()
logger.setLevel(logging.INFO)

s16_resampler = av.AudioResampler(format="s16", rate="16000", layout="mono")
s3_client = boto3.client('s3', aws_access_key_id=heconstants.AWS_ACCESS_KEY,
                         aws_secret_access_key=heconstants.AWS_SECRET_ACCESS_KEY)


class S3SERVICE:
    def __init__(self):
        self.default_bucket = heconstants.ASR_BUCKET

    def upload_to_s3(self, s3_filename, data, bucket_name: Optional[str] = None, is_json: Optional[bool] = False):
        try:
            if bucket_name is None:
                bucket_name = self.default_bucket
            if is_json:
                data = json.dumps(data).encode('utf-8')
            s3_client.put_object(Bucket=bucket_name, Key=s3_filename, Body=data)
            print(f"Upload Successful: {s3_filename}")
        except FileNotFoundError:
            print("The file was not found")
        except NoCredentialsError:
            print("Credentials not available")

    def get_json_file(self, s3_filename, bucket_name: Optional[str] = None):
        try:
            if bucket_name is None:
                bucket_name = self.default_bucket
            s3_object = s3_client.get_object(Bucket=bucket_name, Key=s3_filename)
            file_content = s3_object['Body'].read().decode('utf-8')
            json_data = json.loads(file_content)
            return json_data
        except FileNotFoundError:
            print("The file was not found")
        except NoCredentialsError:
            print("Credentials not available")

    def get_audio_file(self, s3_filename, bucket_name: Optional[str] = None):
        try:
            if bucket_name is None:
                bucket_name = self.default_bucket
            s3_object = s3_client.get_object(Bucket=bucket_name, Key=s3_filename)
            return s3_object
        except FileNotFoundError:
            print("The file was not found")
        except NoCredentialsError:
            print("Credentials not available")

    def check_file_exists(self, key, bucket_name: Optional[str] = None):
        try:
            if bucket_name is None:
                bucket_name = self.default_bucket
            s3_client.head_object(Bucket=bucket_name, Key=key)
            return True
        except s3_client.exceptions.ClientError:
            return False

    def get_files_matching_pattern(self, pattern, bucket_name: Optional[str] = None):
        json_data_list = []
        try:
            if bucket_name is None:
                bucket_name = self.default_bucket
            # Extract the prefix from the pattern (up to the first wildcard)
            prefix = pattern.split('*')[0]

            # Paginate through results if there are more files than the max returned in one call
            paginator = s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
                if 'Contents' in page:
                    # Filter the objects whose keys match the pattern and read each JSON file
                    for obj in page['Contents']:
                        if fnmatch.fnmatch(obj['Key'], pattern):
                            try:
                                response = s3_client.get_object(Bucket=bucket_name, Key=obj['Key'])
                                file_content = response['Body'].read().decode('utf-8')
                                json_data = json.loads(file_content)
                                json_data_list.append(json_data)
                            except NoCredentialsError:
                                print("Credentials not available for file:", obj['Key'])
                            except s3_client.exceptions.ClientError as e:
                                print(f"An error occurred with file {obj['Key']}: {e}")
            json_data_list.sort(key=lambda x: x['chunk_no'])
            return json_data_list
        except Exception as exc:
            self.logger.error(str(exc))
            return []
        except NoCredentialsError:
            print("Credentials not available")
        except s3_client.exceptions.ClientError as e:
            print(f"An error occurred: {e}")
            return []

    def list_files_in_directory(self, directory, bucket_name: Optional[str] = None):
        try:
            if bucket_name is None:
                bucket_name = self.default_bucket
            response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=directory)
            return [item['Key'] for item in response.get('Contents', [])]
        except Exception as e:
            print(f"Error listing files: {e}")

    def download_from_s3(self, key, local_path, bucket_name: Optional[str] = None):
        try:
            if bucket_name is None:
                bucket_name = self.default_bucket
            s3_client.download_file(bucket_name, key, local_path)
            print(f"Download Successful: {local_path}")
        except Exception as e:
            print(f"Error downloading file: {e}")


s3 = S3SERVICE()


def push_logs(care_request_id: str, given_msg: str, he_type: str, req_type: str, source_type: str):
    try:
        headers = {
            'Content-Type': 'application/json'
        }
        websocket_data = {
            "care_request_id": care_request_id,
            "he_type": he_type,
            "req_type": req_type,
            "message": given_msg,
            "source_type": source_type
        }
        response = requests.request("POST", heconstants.HEALIOM_SERVER + "/post_websocket_logs", headers=headers,
                                    data=json.dumps(websocket_data))
        logger.info(f"pushed logs :: {response}")
    except Exception as e:
        logger.info(f"Couldn't push the log to ES :: {e}")
        pass


def retry_with_backoff(function, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            return function()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise e
            backoff_time = 0.1
            time.sleep(backoff_time)


def yield_chunks_from_rtmp_stream(stream_key, user_type, stream_url=heconstants.RTMP_SERVER_URL):
    rtmp_stream = None
    current_position = None
    just_reconnected = False

    # Current position in the stream based on the latest packet PTS received

    def reconnect_to_stream():
        nonlocal just_reconnected, rtmp_stream
        rtmp_stream = retry_with_backoff(
            lambda: av.open(stream_url + stream_key, format="flv", timeout=10)
        )
        logger.info(f"Connection to stream :: {rtmp_stream}")
        just_reconnected = True  # Set the flag to indicate that we have just reconnected

    reconnect_to_stream()

    if rtmp_stream is None:
        logger.info("No RTMP stream found")
        return None

    try:
        bytes_buffer = BytesIO()
        try:
            aac_audio = next((s for s in rtmp_stream.streams if s.type == 'audio'), None)
        except Exception as e:
            logger.error(f"An unexpected error occurred reading rtmp_stream {e}")

        if aac_audio is None:
            logger.error(f"An unexpected error occurred aac_audio {e}")
            raise av.AVError("No audio stream found in RTMP stream.")

        s16_destination = av.open(bytes_buffer, mode="w", format="wav")
        s16_stream = s16_destination.add_stream("pcm_s16le", rate=16000, layout="mono")

        def demux_aac_audio():
            return rtmp_stream.demux(aac_audio)

        while True:
            try:
                for packet in retry_with_backoff(demux_aac_audio):
                    if just_reconnected:
                        # If we just reconnected and the packet's PTS is not ahead of the current position, skip it
                        if current_position is not None and packet.pts <= current_position:
                            continue
                        just_reconnected = False
                    current_position = packet.pts  # Store the PTS to allow checking on reconnection
                    # Packet processing and yielding bytes from the encoded packet...
                    for decoded_packet in packet.decode():
                        for resampled_packet in s16_resampler.resample(decoded_packet):
                            for encoded_packet in s16_stream.encode(resampled_packet):
                                yield bytes(encoded_packet)

            except av.AVError as e:  # Catch specific PyAV exceptions here
                logger.error(f"PyAV exceptions: {e}")
                if rtmp_stream:
                    rtmp_stream.close()
                    rtmp_stream = None
                time.sleep(2)  # Wait before reconnecting
                retry_with_backoff(reconnect_to_stream)
                if rtmp_stream is None:
                    logger.error("Reconnection failed")
                    break  # Implement this function
                else:
                    logger.info(f"PyAV rtmp_stream: {rtmp_stream}")
                    push_logs(care_request_id=stream_key,
                              given_msg="Livestream started (RTMP)",
                              he_type=user_type,
                              req_type="rtmp_restart",
                              source_type="backend")
                continue  # Continue the loop after reconnection

            except av.error.OSError as e:  # Catch specific PyAV exceptions here
                if "Input/output error" in str(e):
                    logger.error("Input/output error. Reconnecting...")
                    logger.error(f"PyAV exceptions: {e}")
                    if rtmp_stream:
                        rtmp_stream.close()
                        rtmp_stream = None
                    time.sleep(2)  # Wait before reconnecting
                    retry_with_backoff(reconnect_to_stream)
                    if rtmp_stream is None:
                        logger.error("Reconnection failed")
                        break
                    else:
                        logger.info(f"PyAV rtmp_stream: {rtmp_stream}")
                        push_logs(care_request_id=stream_key,
                                  given_msg="Livestream started (RTMP)",
                                  he_type=user_type,
                                  req_type="rtmp_restart",
                                  source_type="backend")
                    continue  # Continue the loop after reconnection

            except Exception as e:
                logger.error(f"Connection lost: {e}")
                if rtmp_stream:
                    rtmp_stream.close()
                    rtmp_stream = None
                time.sleep(2)  # Wait before reconnecting
                retry_with_backoff(reconnect_to_stream)
                if rtmp_stream is None:
                    logger.error("Reconnection failed")
                    break  # Implement this function
                else:
                    logger.info(f"rtmp_stream: {rtmp_stream}")
                    push_logs(care_request_id=stream_key,
                              given_msg="Livestream started (RTMP)",
                              he_type=user_type,
                              req_type="rtmp_restart",
                              source_type="backend")
                continue  # Continue the loop after reconnection

        s16_destination.close()
    except Exception as e:
        logger.error(f"An unexpected error occurred  {e}")
        time.sleep(10)
        push_logs(care_request_id=stream_key,
                  given_msg="Livestream stopped (RTMP)",
                  he_type=user_type,
                  req_type="rtmp_stop",
                  source_type="backend")
        return None


def save_rtmp_loop(
        stream_key,
        user_type,
        websocket,
        stream_url=heconstants.RTMP_SERVER_URL,
        DATA_DIR="healiom_websocket_asr",
):
    try:
        transcript = ""
        logger.info(f"WS quick loop received rtmp stream :: {websocket}")
        push_logs(care_request_id=stream_key,
                  given_msg="Livestream started (WS QUICK LOOP)",
                  he_type=user_type,
                  req_type="rtmp_start",
                  source_type="backend")

        stream_url = heconstants.RTMP_SERVER_URL
        rtmp_iterator = yield_chunks_from_rtmp_stream(stream_key, user_type, stream_url)

        if rtmp_iterator is not None:
            started = False
            chunk_count = 1
            frames_per_chunk = 16000 * heconstants.chunk_duration  # N seconds of frames at 16000 Hz
            bytes_per_frame = 2  # Assuming 16-bit audio (2 bytes per frame)

            while True:
                chunk_start_time = time.time()
                chunk_start_datetime = datetime.utcnow()
                wav_buffer = io.BytesIO()
                WAV_F = wave.open(wav_buffer, "wb")
                WAV_F.setnchannels(1)
                WAV_F.setsampwidth(2)
                WAV_F.setframerate(16000)

                frames_written = 0

                for byte_data in rtmp_iterator:
                    if not started:
                        data = {"stream_key": stream_key,
                                "last_processed_end_time": 0,
                                "stage": "rtmp_saving_started"}
                        s3_file = f"{stream_key}/{stream_key}.json"
                        if not s3.check_file_exists(s3_file):
                            s3.upload_to_s3(s3_file, data, is_json=True)
                        logger.info(f"Writing chunks started :: {stream_key}")
                        started = True

                    WAV_F.writeframes(byte_data)
                    frames_written += len(byte_data) // bytes_per_frame

                    current_time = time.time()
                    if current_time - chunk_start_time >= heconstants.chunk_duration and frames_written >= frames_per_chunk:
                        break

                WAV_F.close()
                key = f"{stream_key}/{stream_key}_chunk{chunk_count}.wav"
                wav_buffer.name = key.split("/")[1]
                wav_buffer.seek(0)  # Reset buffer pointer to the beginning
                logger.info(f"sending chunks for transcription :: {key}")
                transcription_result = requests.post(
                    heconstants.AI_SERVER + "/transcribe/infer",
                    files={"f1": wav_buffer},
                ).json()["prediction"][0]
                chunk_count += 1
                segments = transcription_result.get("segments")
                if segments:
                    text = segments[0].get("text")
                    if text:
                        if transcript != "":
                            transcript += "\n" + text
                        else:
                            transcript = text
                try:
                    websocket.send(json.dumps({"transcript": transcript, "segments": [],
                                               "ai_preds": {}, "success": True}))
                    transcript_key = f"{stream_key}/transcript.json"
                    transcript_data = {"transcript": transcript}
                    s3.upload_to_s3(transcript_key, transcript_data, is_json=True)
                    # with Timeout(2, False):  # Set the timeout to 2 seconds
                    #     websocket.receive()

                except Timeout:
                    logger.info("NO ACK RECEIVED CLOSED BY SERVER")
                    push_logs(care_request_id=stream_key,
                              given_msg=f"Websocket has closed by server - NO ACK RECEIVED",
                              he_type=user_type,
                              req_type="websocket_stop",
                              source_type="backend")
                    websocket.close()
                    break

                except Exception as ex:
                    trace = traceback.format_exc()
                    logger.error(f"CLOSED BY CLIENT :: {ex} :: \n {trace}")
                    push_logs(care_request_id=stream_key,
                              given_msg=f"websocket has closed by client",
                              he_type=user_type,
                              req_type="websocket_stop",
                              source_type="backend")
                    websocket.close()
                    break

                if current_time - chunk_start_time < heconstants.chunk_duration:
                    # Break the while loop if the last chunk duration is less than 5 seconds
                    break
        else:
            logger.info("rtmp_iterator IS NONE")

        # esquery
        logger.info("Stopped writing chunks")
        key = f"{stream_key}/{stream_key}.json"
        data = s3.get_json_file(key)
        if data:
            data["stage"] = "rtmp_saving_done"
            s3.upload_to_s3(key, data, is_json=True)

    except Exception as exc:
        msg = "Failed rtmp loop saver :: {}".format(exc)
        trace = traceback.format_exc()
        logger.error(msg, trace)


if __name__ == "__main__":
    import sys

    save_rtmp_loop(sys.argv[1], "patient")