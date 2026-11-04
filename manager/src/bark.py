"""Short-lived screenshot links for Bark's unauthenticated image downloader."""
from itsdangerous import URLSafeTimedSerializer


IMAGE_MAX_AGE = 24 * 60 * 60


def screenshot_signer(secret):
    return URLSafeTimedSerializer(secret, salt="bark-screenshot-v1")
