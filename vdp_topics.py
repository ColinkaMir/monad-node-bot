from dataclasses import dataclass


@dataclass(frozen=True)
class Topic:
    key: str
    category: str
    title: str
    description: str


TOP_LEVEL_BUTTONS = (
    ("VDP Tracker", "Node Monitoring"),
    ("My Alerts", "Help"),
)


VDP_CATEGORIES = {
    "approvals_funding": "Approvals & Funding",
    "registrations": "Registrations",
    "active_set": "Active Set & Rotation",
}


TOPICS = (
    Topic(
        key="vdp.new_foundation_funding_transfer",
        category="approvals_funding",
        title="New Foundation funding transfer",
        description="Alert when a tracked Foundation wallet sends a new validator-start transfer.",
    ),
    Topic(
        key="vdp.new_vdp_approval",
        category="approvals_funding",
        title="New VDP approval",
        description="Alert when a new address appears as approved in the public VDP data set.",
    ),
    Topic(
        key="vdp.new_validator_registration",
        category="registrations",
        title="New validator registration",
        description="Alert when a new validator registration is observed on-chain.",
    ),
    Topic(
        key="vdp.approved_to_registered",
        category="registrations",
        title="Approved to registered",
        description="Alert when a previously approved address completes validator registration.",
    ),
    Topic(
        key="vdp.entered_active_set",
        category="active_set",
        title="Entered active set",
        description="Alert when a tracked validator moves into the live active set.",
    ),
    Topic(
        key="vdp.left_active_set",
        category="active_set",
        title="Left active set",
        description="Alert when a tracked validator leaves the live active set or rotates out.",
    ),
    Topic(
        key="vdp.consensus_stake_added",
        category="active_set",
        title="Consensus stake added",
        description="Alert when a tracked validator gains a meaningful amount of live consensus stake.",
    ),
    Topic(
        key="vdp.consensus_stake_removed",
        category="active_set",
        title="Consensus stake removed",
        description="Alert when a tracked validator loses a meaningful amount of live consensus stake.",
    ),
    Topic(
        key="vdp.snapshot_stake_added",
        category="active_set",
        title="Snapshot stake added",
        description="Alert when a tracked validator gains a meaningful amount of live snapshot stake.",
    ),
    Topic(
        key="vdp.snapshot_stake_removed",
        category="active_set",
        title="Snapshot stake removed",
        description="Alert when a tracked validator loses a meaningful amount of live snapshot stake.",
    ),
    Topic(
        key="vdp.rotation_pair_detected",
        category="active_set",
        title="Possible rotation pair",
        description="Alert when a stake removal and a stake addition of similar size appear in the same poll cycle.",
    ),
)


TOPIC_BY_KEY = {topic.key: topic for topic in TOPICS}


TOPICS_BY_CATEGORY = {
    category_key: [topic for topic in TOPICS if topic.category == category_key]
    for category_key in VDP_CATEGORIES
}
