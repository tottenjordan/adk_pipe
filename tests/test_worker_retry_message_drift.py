"""The CRF worker's retry message is a copy of runserver's AUTO_RETRY_MESSAGE.

The Cloud Function can't import runserver (its image ships only
cloud_functions/creative_fanout), so it keeps a copy; both must stay equal
because creative_agent's <RETRY_AFTER_ERROR> rule is tuned to that text.
Deliberately not named test_crf_*: the crf-deps CI venv has no runserver.
"""

from cloud_functions.creative_fanout.completion import RETRY_MESSAGE
from runserver.async_runs import AUTO_RETRY_MESSAGE


def test_worker_retry_message_matches_runserver():
    assert RETRY_MESSAGE == AUTO_RETRY_MESSAGE
