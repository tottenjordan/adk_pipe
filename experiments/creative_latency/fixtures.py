"""Fixed experiment inputs.

A single, constant campaign message is reused across every trial and every
config so the only thing varying between measurements is the code under test —
not the prompt. The format mirrors what the frontend campaign form sends
(``frontend/src/app/page.tsx``): ``Brand Name`` / ``Target Audience`` /
``Target Product`` / ``Key Selling Points`` (+ ``target_search_trend`` for the
creative agents).
"""

# Kept deliberately generic/evergreen so the trend doesn't age out or skew
# research toward time-sensitive material between runs.
CAMPAIGN_MESSAGE = (
    'Brand Name: "PRS Guitars"\n'
    'Target Audience: "Aspiring and intermediate electric guitarists, ages 18-35"\n'
    'Target Product: "PRS SE CE 24 electric guitar"\n'
    'Key Selling Points: "Wide tonal range, bolt-on maple neck, '
    'pro-level build quality at an accessible price"\n'
    'target_search_trend: "summer music festival season"'
)

# The app (agent) under measurement.
APP_NAME = "creative_agent"
