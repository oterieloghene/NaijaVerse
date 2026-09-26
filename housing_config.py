"""
housing_config.py

House-thread layout for !assignhouse / !evicthouse, keyed by the same codes as
document_config.HOUSE_TYPES. Only ordinary house types live here — the two
government residences (governor-penthouse, president-villa) are handled
entirely by the existing !permit flow and are never touched by the housing
commands, so they aren't listed below.

Each entry:
    private: [(room_key, room_label), ...] — one private thread per resident.
        A room_key in NAMED_ROOM_KEYS ("parlour" for ordinary houses, "room"
        for bed-sitter) folds the resident's display name into the thread
        name ("Ada Parlour", "Ada Bedsitter"). Every other room is named
        plainly ("Bathroom", "Kitchen", "Bedroom", "Bedroom 1", ...) with a
        pinned message inside naming the resident, since the plain thread
        name alone doesn't say whose room it is.
    shared: [shared_key, ...] — threads every resident of this house type in
        a state is tagged into together. Created once per (state, house_type,
        shared_key) and reused — never deleted when one resident is evicted,
        only untagged. private-estate's "pool" and luxury-duplex's "pool" are
        two separate threads: they live under two different parent channels.
"""

RENT_DAYS = 7

NAMED_ROOM_KEYS = {"parlour", "room"}

HOUSE_ROOMS = {
    "bed-sitter": {
        "private": [("room", "Bedsitter")],
        "shared": [],
    },
    "line-houses": {
        "private": [],
        "shared": ["face_me_i_face_you", "bathroom"],
    },
    "mini-flat": {
        "private": [("parlour", "Parlour"), ("bathroom", "Bathroom"), ("bedroom", "Bedroom")],
        "shared": [],
    },
    "two-bedroom-flat": {
        "private": [("parlour", "Parlour"), ("bathroom", "Bathroom"), ("kitchen", "Kitchen"),
                    ("bedroom", "Bedroom")],
        "shared": [],
    },
    "three-bedroom-flat": {
        "private": [("parlour", "Parlour"), ("bathroom", "Bathroom"), ("kitchen", "Kitchen"),
                    ("bedroom_1", "Bedroom 1"), ("bedroom_2", "Bedroom 2")],
        "shared": [],
    },
    "private-estate": {
        "private": [("parlour", "Parlour"), ("bathroom", "Bathroom"), ("kitchen", "Kitchen"),
                    ("bedroom_1", "Bedroom 1"), ("bedroom_2", "Bedroom 2")],
        "shared": ["pool"],
    },
    "luxury-duplex": {
        "private": [("parlour", "Parlour"), ("bathroom", "Bathroom"), ("kitchen", "Kitchen"),
                    ("bedroom_1", "Bedroom 1"), ("bedroom_2", "Bedroom 2")],
        "shared": ["pool"],
    },
    "penthouse": {
        "private": [("parlour", "Parlour"), ("bathroom", "Bathroom"), ("kitchen", "Kitchen"),
                    ("bedroom_1", "Bedroom 1"), ("bedroom_2", "Bedroom 2"), ("bedroom_3", "Bedroom 3"),
                    ("pool", "Swimming Pool")],
        "shared": [],
    },
}


def private_thread_name(room_key, room_label, display_name):
    if room_key in NAMED_ROOM_KEYS:
        return f"{display_name} {room_label}"
    return room_label


def shared_thread_name(shared_key):
    return shared_key.replace("_", " ").title()
