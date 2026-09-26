"""
guest_pass_config.py

Settings for the Estate app's guest-pass system (cogs/estate.py):

  Apply for Pass -> Housing Officer approval (staff-office) -> single-use
  4-digit code, relayed to the visitor by the officer -> Redeem Code ->
  visitor role (if the house type has one) + tagged into the threads the
  owner picked -> clock starts on redemption, not approval.

VISITOR_ROLE_BY_HOUSE_TYPE: low-cost housing (line-houses, bed-sitter) has
no visitor role at all — those channels are already open to any state
resident — so redemption there only tags threads, no role change.
"""

COOLDOWN_MINUTES = 10          # after an AUTO-expired pass, this owner can't re-invite this visitor
CODE_LENGTH = 4
AUTO_EXPIRE_POLL_SECONDS = 60
MAX_REQUESTED_MINUTES = 10080  # 7 days, same ceiling as thread auto-archive

VISITOR_ROLE_BY_HOUSE_TYPE = {
    "bed-sitter": None,
    "line-houses": None,
    "mini-flat": "Mid-Class Visitor",
    "two-bedroom-flat": "Mid-Class Visitor",
    "three-bedroom-flat": "Mid-Class Visitor",
    "private-estate": "High-Class Visitor",
    "luxury-duplex": "High-Class Visitor",
    "penthouse": "High-Class Visitor",
}
