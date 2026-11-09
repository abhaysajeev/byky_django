"""What the gateway's database role may do -- the same list as
deploy/tracking-role.sql and migration 0002 (tests check it with a
throw-away role: apps/tracking/tests/test_gateway_write.py)."""

GRANTS = """
GRANT SELECT, INSERT ON tracking_reading, tracking_reading_key, tracking_raw_frame TO {role};
GRANT SELECT, INSERT, UPDATE ON tracking_tag_last_seen TO {role};
GRANT SELECT, INSERT, UPDATE ON tracking_reader TO {role};
GRANT SELECT ON tracking_antenna, tracking_reader_assignment, tracking_calibration TO {role};
GRANT USAGE ON SEQUENCE tracking_reader_id_seq, tracking_raw_frame_id_seq TO {role};
"""


def grants_for(role):
    return GRANTS.format(role=role)
