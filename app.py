from flask import Flask, request
import requests
import logging
import os

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)

# Shlink API details
SHLINK_URL = os.environ.get('SHLINK_URL')
SHLINK_API_KEY = os.environ.get('SHLINK_API_KEY')


@app.route("/", methods=["GET"])
def translate():

    link = request.args.get('link')

    # Add the Shlink API key to the headers
    headers = {
        'Content-Type': 'application/json',
        'X-Api-Key': SHLINK_API_KEY
    }
    
    request_body = {
        'longUrl': link,
        'maxVisits': 100,
        'tags': ['PrivateBin'],
        'crawlable': False
    }

    # Make the POST request to Shlink
    response = requests.post(SHLINK_URL, json=request_body, headers=headers)
    
    # Convert the response to JSON
    response_json = response.json()

    logging.info(response_json)
    
    # Return the shortUrl from the response
    return response_json['shortUrl']


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)