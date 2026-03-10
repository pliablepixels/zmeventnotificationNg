import os
import firebase_admin
from firebase_admin import messaging
from firebase_admin import exceptions
from firebase_admin import credentials
from flask import jsonify
import json
from firebase_admin import auth
from functools import wraps
import jwt
import re

# Imports the Cloud Logging client library
import logging
import google.cloud.logging as cloudlogging
log_client = cloudlogging.Client()
log_handler = log_client.get_default_handler()
cloud_logger = logging.getLogger("cloudLogger")
cloud_logger.setLevel(logging.INFO)
cloud_logger.addHandler(log_handler)

# Will only log if the user passes a log_raw_message field
_ENABLE_CONDITIONAL_RAW_LOGS=True

if not firebase_admin._apps:
    default_app = firebase_admin.initialize_app()


def authenticated(fn):
    @wraps(fn)
    def wrapped(request):
        SECRET='<?PLACE SECRET HERE?>'
        try:
            # Extract the firebase token from the HTTP header
            token = request.headers['Authorization']
            token = token.replace('Bearer ','')
            # Validate the token
            verified = jwt.decode(token, SECRET, algorithms=['HS256'])
        except Exception as e:
            # If an exception occured above, reject the request
            result = {'Error': 'Invalid credentials: {}'.format(e)}
            return jsonify(result), 401
        # Execute the authenticated function
        return fn(request)
    # Return the input function "wrapped" with our
    # authentication check, i.e. fn(authenticated(request))
    return wrapped

# payload:
# https://firebase.google.com/docs/reference/admin/python/firebase_admin.messaging
@authenticated
def send_push(request):

    android_payload = None
    ios_payload = None
    data_payload = {}

    request_json = request.get_json(silent=True)
    if not request_json:
        result = {'Error': 'No JSON Found'}
        return jsonify(result), 400
    if not request_json.get('token'):
        result = {'Error': 'No Token Found'}
        return jsonify(result), 400

    log_platform = ''
    log_token = request_json.get('token')[-10:]
    log_image = ''
    log_message = {}

    if request_json.get('android') and request_json.get('android').get('icon'):
        android_segment = request_json.get('android', {})
        android_priority = android_segment.get('priority', 'high')
        if not android_priority in ['high', 'normal']:
            android_priority='high'
        android_ttl=None
        if android_segment.get('ttl'):
            android_ttl=int(android_segment.get('ttl'))
        log_platform +='android '
        log_message['android'] = {
            'channel': android_segment.get('channel','zmninja-ng'),
            'icon': android_segment.get('icon','ic_stat_notification'),
            'priority': android_priority,
            'ttl': android_ttl,
            'image': '<present>' if request_json.get('image_url') else '<not present>',
            'tag': android_segment.get('tag')

        }

        android_payload = messaging.AndroidConfig(
                priority = android_priority,
                ttl = android_ttl,
                notification=messaging.AndroidNotification(
                    channel_id=android_segment.get('channel','zmninja-ng'),
                    icon=android_segment.get('icon','ic_stat_notification'),
                    priority=android_priority,
                    image=request_json.get('image_url'),
                    tag = android_segment.get('tag'),
                    default_vibrate_timings = True,
                    default_sound = True,
                    default_light_settings = True
            )
        )

    ios_segment = request_json.get('ios', {})
    if ios_segment or not request_json.get('android'):
        log_platform += 'ios '

        log_message['ios'] = {
            'headers': ios_segment.get('headers', {'apns-priority': '10'}),
            'thread_id': ios_segment.get('thread_id'),
            'image': '<present>' if request_json.get('image_url') else '<not present>'
        }

        ios_fcm_options = None
        if request_json.get('image_url'):
            ios_fcm_options = messaging.APNSFCMOptions(
                image=request_json.get('image_url')
            )

        aps_alert = None
        if ios_segment.get('subtitle'):
            aps_alert = messaging.ApsAlert(
                title=request_json.get('title', 'alarm'),
                body=request_json.get('body', 'alarm'),
                subtitle=ios_segment.get('subtitle')
            )

        ios_payload=messaging.APNSConfig(
            headers = ios_segment.get('headers', {'apns-priority': '10'}),
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert = aps_alert,
                    mutable_content = True,
                    badge = request_json.get('badge'),
                    sound = ios_segment.get('sound','default'),
                    custom_data = ios_segment.get('aps_custom_data'),
                    thread_id = ios_segment.get('thread_id')
                ) #Aps
            ), #APNSPayload
            fcm_options=ios_fcm_options
        ) #APNSConfig


    data_payload = request_json.get('data')

    if ios_payload and request_json.get('image_url'):
        data_payload['image_url_jpg'] = request_json.get('image_url')

    # Now iterate and convert all data to string
    for k in data_payload:
        data_payload[k] = str(data_payload[k])

    message = messaging.Message(
        notification=messaging.Notification(
            title=request_json.get('title', 'alarm'),
            body=request_json.get('body', 'alarm'),
            image=request_json.get('image_url'),
        ),
        token=request_json.get('token'),
        apns = ios_payload,
        android=android_payload,
        data = data_payload
    )

    log_message['non_platform_specific'] = {
        'log_id': request_json.get('log_message_id','NONE'),
        'title': request_json.get('title')[:10]+'...' if request_json.get('title') else 'NO',
        'body': request_json.get('body')[:10]+'...' if request_json.get('body') else 'NO',
        'log_raw_message': request_json.get('log_raw_message', 'NO')
    }

    try:
        final_message = 'token: ...{} os:{} image:{} key message elements:{}'.format(log_token, log_platform, log_image, log_message)
        cloud_logger.info (final_message)
    except Exception as e:
        cloud_logger.error ('Error creating log message {}'.format(e))

    if _ENABLE_CONDITIONAL_RAW_LOGS and request_json.get('log_raw_message'):
        try:
            dmessage = '{}'.format(message)
            dmessage = re.sub('&user(name)?=.*?&', '&username=something&',dmessage)
            dmessage = re.sub('&pass(word)?=.*?[},$]', '&password=something',dmessage)
            cloud_logger.info ('RAW message ID:{} being sent to FCM:{}'.format( request_json.get('log_message_id','NONE'), dmessage))
        except Exception as e:
            cloud_logger.error ('Error creating log message {}'.format(e))


    try:
        response = messaging.send(message)
    except exceptions.FirebaseError as ex:
        str_error = "token:{}=>{}".format(log_token, ex)
        result = {'Error': str_error}
        cloud_logger.error(str_error)
        return jsonify(result), 400
    else:
        result = {'Success': response}
        cloud_logger.info(result)
        return jsonify(result), 200
