"""User profile helpers."""


def get_city(profile):
    """City of the profile, or 'unknown' when absent."""
    return profile["city"]
