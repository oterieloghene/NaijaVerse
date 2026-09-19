"""
locations.py

Hardcoded location hierarchy for the RP bot, with role-gating baked in.

Structure:
    LOCATIONS[state][category_code] = {
        "display_name": str,
        "locations": {
            location_code: {
                "name": str,
                "access": ACCESS,
                "non_physical": bool,
                "voice_channel": bool,
                "flight_only": bool,
                "sub_locations": {
                    sub_code: {
                        "name": str,
                        "access": ACCESS,
                        "non_physical": bool,
                        "voice_channel": bool,
                    },
                    ...
                }
            },
            ...
        }
    }

ACCESS format
    A list of groups. Each group is a list of role names.
        - Groups are alternatives (OR): meeting ANY one group grants access.
        - Roles inside a group are ALL required (AND).
    Example: [["Police Officer", "Lagos Employee"], ["Jail Visitor"]]
        -> (Police Officer AND Lagos Employee) OR Jail Visitor
    access=None means the location is ungated (anyone may enter).
    access=[] would mean nobody can enter, so it is never used.

Role names are already resolved per state (e.g. "Lagos Employee"), so the
bare state role is just the state name ("Lagos"). Matching is case-insensitive.

OVERSEAS_LOCATIONS is separate — not tied to any state, flight-only.
"""

from functools import partial

STATES = ["Lagos", "Delta", "Abuja"]

# Short codes used on NIN numbers, e.g. NIN0001DL
STATE_CODES = {"Delta": "DL", "Lagos": "LA", "Abuja": "FCT"}

# Petroleum commissioner is named differently per state.
PETROLEUM_COMMISSIONER = {
    "Lagos": "Lagos Commissioner of Petroleum",
    "Delta": "Delta Commissioner of Petroleum and Natural Resources",
    "Abuja": "Abuja Commissioner of Petroleum and Natural Resources",
}


# ---------------------------------------------------------------------------
# GUEST ROLES (Governor's House)
# These are plain Discord roles — no per-host roles are ever created. Which
# governor/deputy granted the access (and for which state) is stored in the
# database (guest_passes table, see database.py), not in the role name.
# Role -> pass_type stored in the database:
# ---------------------------------------------------------------------------

STATE_VISITOR_GUEST = "State Visitor/Guest"
STATE_GUEST = "State Guest"
GUEST_PASS_ROLES = {STATE_VISITOR_GUEST: "visitor", STATE_GUEST: "guest"}


def _A(state, *groups):
    """
    Build an ACCESS list for a state.
    Each argument is one alternative (OR): a role string, or a tuple of roles
    that must all be held (AND). "{S}" is replaced with the state name.
    """
    out = []
    for group in groups:
        if isinstance(group, str):
            group = (group,)
        out.append([role.replace("{S}", state) for role in group])
    return out


def _loc(name, access=None, non_physical=False, voice_channel=False,
         flight_only=False, sub_locations=None):
    return {
        "name": name,
        "access": access,
        "non_physical": non_physical,
        "voice_channel": voice_channel,
        "flight_only": flight_only,
        "sub_locations": sub_locations or {},
    }


def _sub(name, access=None, non_physical=False, voice_channel=False):
    return {
        "name": name,
        "access": access,
        "non_physical": non_physical,
        "voice_channel": voice_channel,
    }


# Shared role names (use "{S}" for the state)
RES = "{S}"                                   # bare state role
EMP = "{S} Employee"
OFFICIAL = "{S} Government Official"
FED_OFFICIAL = "Federal Government Official"


def _with_emp(*roles):
    """Each role must be held together with the state Employee role."""
    return [(r, EMP) for r in roles]


def _state_government(S, A):
    locs = {}
    if S == "Abuja":
        # No governor in Abuja — the FCT Minister takes their place.
        locs["fct-minister"] = _loc("FCT Minister", A("Meeting with FCT Minister", "FCT Minister"))
    else:
        locs["governor-office"] = _loc("Governor Office", A("{S} Governor", "Meeting with Governor"))
        locs["deputy-governor-office"] = _loc("Deputy Governor Office", A("{S} Deputy Governor", "Meeting with Deputy"))
        locs["chief-of-staff"] = _loc("Chief of Staff", A(OFFICIAL, FED_OFFICIAL))

    locs["state-secretariat"] = _loc("State Secretariat", A(OFFICIAL, FED_OFFICIAL))
    locs["council"] = _loc("Council", A(OFFICIAL, ("Top Government Official", EMP)), sub_locations={
        "ministry-of-finance": _sub("Ministry of Finance", A(OFFICIAL, FED_OFFICIAL)),
        "ministry-of-justice": _sub("Ministry of Justice", A(OFFICIAL, FED_OFFICIAL)),
        "ministry-of-agriculture": _sub("Ministry of Agriculture", A(OFFICIAL, FED_OFFICIAL)),
        "ministry-of-housing": _sub("Ministry of Housing", A(OFFICIAL, FED_OFFICIAL)),
        "ministry-of-education": _sub("Ministry of Education", A(OFFICIAL, ("Top Government Official", EMP), FED_OFFICIAL)),
        "ministry-of-commerce": _sub("Ministry of Commerce", A(OFFICIAL, FED_OFFICIAL)),
        "ministry-of-power-and-water": _sub("Ministry of Power and Water", A(OFFICIAL)),
        "ministry-of-petroleum": _sub("Ministry of Petroleum", A(OFFICIAL, FED_OFFICIAL)),
        # Literal channel name is "council" but this one is the voice channel
        "council-voice": _sub("Council", A(OFFICIAL, ("Top Government Official", EMP)), voice_channel=True),
    })
    locs["city-hall"] = _loc("City Hall", A(RES), voice_channel=True)

    return {"display_name": "State Government", "locations": locs}


def _governors_house(S, A):
    """Delta and Lagos only — Abuja has no governor to house."""
    resident_gov = ("State Resident", "{S} Governor")
    resident_dep = ("State Resident", "{S} Deputy Governor")
    VISITOR = STATE_VISITOR_GUEST
    GUEST = STATE_GUEST
    return {
        "display_name": "Governor's House",
        "locations": {
            "governor-penthouse": _loc("Governor Penthouse", A(resident_gov, resident_dep, VISITOR), sub_locations={
                "deputy-governor-residence": _sub("Deputy Governor Residence", A(resident_gov, resident_dep, VISITOR)),
                "governor-guesthouse": _sub("Governor Guesthouse", A(resident_gov, resident_dep, GUEST)),
            }),
        },
    }


def _common_categories(S):
    """Categories shared by Lagos, Delta, and Abuja, with roles resolved for state S."""
    A = partial(_A, S)
    petroleum = PETROLEUM_COMMISSIONER[S]

    categories = {
        "border_entry": {
            "display_name": "Border & Entry",
            "locations": {
                "arrival-terminal": _loc("Arrival Terminal", A("{S} Arrival", RES), non_physical=True),
                "airport": _loc("Airport", A(RES), non_physical=True),
                "immigration-office": _loc("Immigration Office", A(RES, "{S} Arrival"), sub_locations={
                    "refugee-camp": _sub("Refugee Camp", A("{S} Arrival")),
                    "travel-agency": _sub("Travel Agency", A(RES)),
                    "front-desk": _sub("Front Desk", A("Immigration Officer")),
                    "parcel-pickup": _sub("Parcel Pickup", A("Immigration Officer", "Dispatch Rider")),
                    "chief-marshal-office": _sub("Chief Marshal Office", A("Chief Immigration Officer")),
                }),
            },
        },
        "hotel_suites": {
            "display_name": "Hotel & Suites",
            "locations": {
                "hotel-reception": _loc("Hotel Reception", A(RES)),
            },
        },
        "broadcasting_station": {
            "display_name": "Broadcasting Station",
            "locations": {
                "broadcasting-station": _loc("Broadcasting Station", A(RES), sub_locations={
                    "production-room": _sub("Production Room", A(("Media Staff", EMP))),
                    "media-manager": _sub("Media Manager", A(("Broadcast Manager", EMP))),
                }),
            },
        },
        "judiciary": {
            "display_name": "Judiciary",
            "locations": {
                "clerk-office": _loc("Clerk Office", A(RES), sub_locations={
                    "lawyer-chambers": _sub("Lawyer Chambers", A(("Lawyer", EMP))),
                    "judge-chambers": _sub("Judge Chambers", A(("High Judge", EMP))),
                    "court-room": _sub("Court Room", A(RES), voice_channel=True),
                }),
            },
        },
        "state_government": _state_government(S, A),
    }

    if S != "Abuja":
        categories["governors_house"] = _governors_house(S, A)

    categories.update({
        "bank_plc": {
            "display_name": "Bank Plc",
            "locations": {
                "banking-hall": _loc("Banking Hall", A(RES), sub_locations={
                    "atm": _sub("ATM", A(RES)),
                    "banking-office": _sub("Banking Office", A(*_with_emp("Bank Staff"))),
                    "deposit": _sub("Deposit", A(*_with_emp("Bank Staff"))),
                    "auditor": _sub("Auditor", A(*_with_emp("Bank Staff"))),
                    "bank-manager": _sub("Bank Manager", A(*_with_emp("Bank Staff"))),
                    "executive-director": _sub("Executive Director", A(*_with_emp("Executive Director"))),
                    "transaction-log": _sub("Transaction Log", A(*_with_emp("Executive Director", "Bank Manager", "Auditor")), non_physical=True),
                    "treasury": _sub("Treasury", A(*_with_emp("Executive Director"), "{S} Commissioner of Finance"), non_physical=True),
                }),
            },
        },
        "police_department": {
            "display_name": "Police Department",
            "locations": {
                "police-station": _loc("Police Station", A(RES), sub_locations={
                    "officers-office": _sub("Officers Office", A(*_with_emp("Police Officer"))),
                    "investigation-room": _sub("Investigation Room", A(*_with_emp("Police Officer"))),
                    "holding-cell": _sub("Holding Cell", A(*_with_emp("Police Officer"), "Jail Visitor")),
                    "patrol": _sub("Patrol", A(*_with_emp("Police Officer"))),
                    "forensics": _sub("Forensics", A(*_with_emp("Police Officer"))),
                    "armoury": _sub("Armoury", A(*_with_emp("Police Officer"))),
                    "dcp-office": _sub("DCP Office", A(*_with_emp("Police Officer"))),
                    "cp-office": _sub("CP Office", A(*_with_emp("Chief Police Officer"))),
                    "police-radio": _sub("Police Radio", A("Police Officer"), non_physical=True),
                }),
            },
        },
        "hospital": {
            "display_name": "Abuja Specialist Hospital" if S == "Abuja" else f"{S} General Hospital",
            "locations": {
                "hospital-lobby": _loc("Hospital Lobby", A(RES), sub_locations={
                    "hospital-reception": _sub("Hospital Reception", A("Healed", "Out Patient", "Hospital Visitor", *_with_emp("Medic Staff"))),
                    "nursing-station": _sub("Nursing Station", A(*_with_emp("Medic Staff"))),
                    "emergency": _sub("Emergency", A(*_with_emp("Medic Staff"))),
                    "pharmacy-and-laboratory": _sub("Pharmacy and Laboratory", A(*_with_emp("Medic Staff"))),
                    "consultation": _sub("Consultation", A(*_with_emp("Medic Staff"), "Out Patient")),
                    "dentistry": _sub("Dentistry", A(*_with_emp("Dentist"), "Dental Patient", "Chief Medical Director", "Deputy Medical Director")),
                    "female-wards": _sub("Female Wards", A(("Hospitalised", "Female"), *_with_emp("Medic Staff"), "Hospital Visitor")),
                    "male-wards": _sub("Male Wards", A(("Hospitalised", "Male"), *_with_emp("Medic Staff"), "Hospital Visitor")),
                    "surgical-theatre": _sub("Surgical Theatre", A(*_with_emp("Surgeon", "Chief Medical Director", "Deputy Medical Director"), "Pre-operative Patient")),
                    "dmd-office": _sub("DMD Office", A(*_with_emp("Medic Staff"))),
                    "cmd-office": _sub("CMD Office", A(*_with_emp("Chief Medical Director"))),
                }),
            },
        },
        "property_and_development": {
            "display_name": "Property and Development Department",
            "locations": {
                "rental-desk": _loc("Rental Desk", A(RES), sub_locations={
                    "staff-office": _sub("Staff Office", A(*_with_emp("Housing Officer"))),
                }),
            },
        },
        "electricity_water": {
            "display_name": "Electricity and Water Distribution Company",
            "locations": {
                "help-desk": _loc("Help Desk", A(RES), sub_locations={
                    "technician-office": _sub("Technician Office", A(*_with_emp("Technician"))),
                    "operations": _sub("Operations", A(*_with_emp("Commissioner of Power and Water Resources"))),
                    "billing": _sub("Billing", A("{S} Resident", *_with_emp("Technician")), non_physical=True),
                    "fault-report": _sub("Fault Report", A(*_with_emp("Technician")), non_physical=True),
                }),
            },
        },
        "low_cost_housing": {
            "display_name": "Low-Cost Housing",
            "locations": {
                "line-houses": _loc("Line Houses", A(RES)),
                "bed-sitter": _loc("Bed-Sitter", A(RES)),
            },
        },
        "mid_class_residential": {
            "display_name": "Mid-Class Residential",
            "locations": {
                "mini-flat": _loc("Mini Flat", A("Miniflat Resident", "Mid-Class Visitor")),
                "two-bedroom-flat": _loc("Two Bedroom Flat", A("Two Bedroom Flat Resident", "Mid-Class Visitor")),
                "three-bedroom-flat": _loc("Three Bedroom Flat", A("Three Bedroom Flat Resident", "Mid-Class Visitor")),
            },
        },
        "high_class_residential": {
            "display_name": "High-Class Residential",
            "locations": {
                "private-estate": _loc("Private Estate", A("Private Estate Resident", "High-Class Visitor")),
                "luxury-duplex": _loc("Luxury Duplex", A("Luxury Duplex Resident", "High-Class Visitor")),
                "penthouse": _loc("Penthouse", A("Penthouse Resident", "High-Class Visitor")),
            },
        },
        "career_high_school": {
            "display_name": "Career High School",
            "locations": {
                "school-building": _loc("School Building", A(RES)),
            },
        },
        "market_district": {
            "display_name": "Market District",
            "locations": {
                "market-district": _loc("Market District", A(RES), sub_locations={
                    "main-market": _sub("Main Market", A(RES)),
                    "abattoir": _sub("Abattoir", A(RES)),
                }),
            },
        },
        "automotive_district": {
            "display_name": "Automotive District",
            "locations": {
                "automotive-district": _loc("Automotive District", A(RES), sub_locations={
                    "nnpc-fuel-station": _sub("NNPC Fuel Station", A(RES)),
                    "auto-repair": _sub("Auto Repair", A(RES)),
                    "dealership": _sub("Dealership", A(RES)),
                    "driving-school": _sub("Driving School", A(RES)),
                }),
            },
        },
        "transport_district": {
            "display_name": "Transport District",
            "locations": {
                "transport-district": _loc("Transport District", A(RES), sub_locations={
                    "bus-park": _sub("Bus Park", A(RES)),
                    "taxi-company": _sub("Taxi Company", A(RES)),
                }),
            },
        },
        "commercial_district": {
            "display_name": "Commercial District",
            "locations": {
                "commercial-district": _loc("Commercial District", A(RES), sub_locations={
                    "mall": _sub("Mall", A(RES)),
                    "club-house": _sub("Club House", A(RES)),
                    "resort": _sub("Resort", A(RES)),
                }),
            },
        },
        "industrial_district": {
            "display_name": "Industrial District",
            "locations": {
                "industrial-district": _loc(
                    "Industrial District",
                    A(("Supplier", RES), "{S} Commissioner of Commerce", petroleum),
                    sub_locations={
                        "depot": _sub("Depot", A("Supplier", "{S} Commissioner of Commerce")),
                        "industrial-goods": _sub("Industrial Goods", A("{S} Commissioner of Commerce")),
                        "food-beverages-industry": _sub("Food & Beverages Industry", A("{S} Commissioner of Commerce")),
                        "refinery": _sub("Refinery", A(
                            petroleum,
                            *(["Delta Governor", "Delta Deputy Governor", "Delta Chief of Staff"] if S == "Delta" else []),
                        )),
                    },
                ),
            },
        },
        "rural_area": {
            "display_name": "Rural Area",
            "locations": {
                # sub_locations and the parent's access are filled per state below.
                "rural-district": _loc("Rural District", None, sub_locations={}),
            },
        },
    })

    return categories


# ---------------------------------------------------------------------------
# Build LOCATIONS for all three states from the shared template
# ---------------------------------------------------------------------------

LOCATIONS = {state: _common_categories(state) for state in STATES}

# --- Rural area sub-locations vary by state ---
# farmland: Delta and Abuja only | ranch: Delta and Abuja only | river: Delta and Lagos only
def _rural(state):
    A = partial(_A, state)
    petroleum = PETROLEUM_COMMISSIONER[state]
    ag = "{S} Commissioner of Agriculture"
    subs = {}
    if state in ("Delta", "Abuja"):
        subs["farmland"] = _sub("Farmland", A("Farmer", "Trader", ag))
        subs["ranch"] = _sub("Ranch", A("Rancher", "Butcher", ag))
    if state in ("Delta", "Lagos"):
        subs["river"] = _sub("River", A(ag, petroleum, "Fisherman", "Butcher"))
    return subs


for _state in STATES:
    _rural_district = LOCATIONS[_state]["rural_area"]["locations"]["rural-district"]
    _rural_district["sub_locations"] = _rural(_state)
    # The parent is open to every role that can enter any of its sub-locations.
    _groups = []
    for _sub_loc in _rural_district["sub_locations"].values():
        for _group in _sub_loc["access"]:
            if _group not in _groups:
                _groups.append(_group)
    _rural_district["access"] = _groups

# ---------------------------------------------------------------------------
# Abuja-only categories
# ---------------------------------------------------------------------------

_A_ABUJA = partial(_A, "Abuja")

LOCATIONS["Abuja"]["aso_rock"] = {
    "display_name": "Aso Rock",
    "locations": {
        "president-office": _loc("President Office", _A_ABUJA("President", "Meeting with President")),
        "vice-president-office": _loc("Vice President Office", _A_ABUJA("Vice President", "Meeting with Vice")),
        "chief-of-staff": _loc("Chief of Staff", _A_ABUJA(OFFICIAL, FED_OFFICIAL)),
        "president-villa": _loc("President Villa", _A_ABUJA("Federal Resident", "Federal Visitor/Guest", "Villa Staff"), sub_locations={
            "vice-president-residence": _sub("Vice President Residence", _A_ABUJA("Federal Resident", "Federal Visitor/Guest", "Villa Staff")),
            "villa-guesthouse": _sub("Villa Guesthouse", _A_ABUJA("Federal Resident", "Federal Guest", "Villa Staff")),
        }),
    },
}

LOCATIONS["Abuja"]["central_bank"] = {
    "display_name": "Central Bank of Nigeria",
    "locations": {
        "cbn-lobby": _loc("CBN Lobby", _A_ABUJA(FED_OFFICIAL), sub_locations={
            "vault": _sub("Vault", _A_ABUJA("CBN Governor", "CBN Deputy")),
            "cbn-deputy": _sub("CBN Deputy", _A_ABUJA("CBN Governor", "CBN Deputy")),
            "cbn-governor": _sub("CBN Governor", _A_ABUJA("CBN Governor")),
            "national-treasury": _sub("National Treasury", _A_ABUJA("CBN Governor", "Minister of Finance"), non_physical=True),
        }),
    },
}

# ---------------------------------------------------------------------------
# State-specific universities (one each, different name/sub-locations)
# ---------------------------------------------------------------------------

_A_DELTA = partial(_A, "Delta")
_A_LAGOS = partial(_A, "Lagos")

LOCATIONS["Delta"]["university"] = {
    "display_name": "Delta State University",
    "locations": {
        "administrative-block": _loc("Administrative Block", _A_DELTA(RES), sub_locations={
            "school-of-law": _sub("School of Law", _A_DELTA("Law Student", "Lecturer")),
            "school-of-nursing": _sub("School of Nursing", _A_DELTA("Nursing Student", "Lecturer")),
            "school-of-admin": _sub("School of Admin", _A_DELTA("Admin Student", "Lecturer")),
            "campus-residence": _sub("Campus Residence", _A_DELTA(
                ("Campus Resident", "Nursing Student"),
                ("Campus Resident", "Law Student"),
                ("Campus Resident", "Admin Student"),
            )),
        }),
    },
}

LOCATIONS["Lagos"]["university"] = {
    "display_name": "Lagos Defence Academy",
    "locations": {
        "administrative-block": _loc("Administrative Block", _A_LAGOS(RES), sub_locations={
            "training-ground": _sub("Training Ground", _A_LAGOS("Cadet", "Police Officer")),
            "lecture-area": _sub("Lecture Area", _A_LAGOS("Cadet", "Police Officer")),
            "campus-residence": _sub("Campus Residence", _A_LAGOS(("Campus Resident", "Cadet"))),
        }),
    },
}

LOCATIONS["Abuja"]["university"] = {
    "display_name": "Abuja School of Banking and Medicine",
    "locations": {
        "administrative-block": _loc("Administrative Block", _A_ABUJA(RES), sub_locations={
            "school-of-medicine": _sub("School of Medicine", _A_ABUJA("Medicine Student", "Lecturer")),
            "school-of-banking": _sub("School of Banking", _A_ABUJA("Banking Student", "Lecturer")),
            "campus-residence": _sub("Campus Residence", _A_ABUJA(
                ("Campus Resident", "Banking Student"),
                ("Campus Resident", "Medicine Student"),
            )),
        }),
    },
}

# ---------------------------------------------------------------------------
# State-specific business locations
# ---------------------------------------------------------------------------

_DELTA_OIL = [PETROLEUM_COMMISSIONER["Delta"], "Delta Governor", "Delta Deputy Governor", "Delta Chief of Staff"]

LOCATIONS["Delta"]["state_specific_business"] = {
    "display_name": "State-Specific Business Locations",
    "locations": {
        "oil-well": _loc("Oil Well", _A_DELTA(*_DELTA_OIL)),
    },
}

LOCATIONS["Lagos"]["state_specific_business"] = {
    "display_name": "State-Specific Business Locations",
    "locations": {
        "sea-port": _loc("Sea Port", _A_LAGOS(OFFICIAL, FED_OFFICIAL)),
    },
}

LOCATIONS["Abuja"]["state_specific_business"] = {
    "display_name": "State-Specific Business Locations",
    "locations": {
        "mining-ground": _loc("Mining Ground", _A_ABUJA(PETROLEUM_COMMISSIONER["Abuja"], "FCT Minister")),
        "kanji-dam": _loc("Kanji Dam", _A_ABUJA("Minister of Power and Water Resources")),
    },
}

# ---------------------------------------------------------------------------
# Overseas — flight-only, not tied to any state
# ---------------------------------------------------------------------------

OVERSEAS_LOCATIONS = {
    "dubai": _loc("Dubai", [["Dubai"]], flight_only=True),
    "seychelles": _loc("Seychelles", [["Seychelles"]], flight_only=True),
    "maldive": _loc("Maldive", [["Maldives"]], flight_only=True),
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def get_category(state, category_code):
    return LOCATIONS.get(state, {}).get(category_code)


def get_location(state, category_code, location_code):
    category = get_category(state, category_code)
    if not category:
        return None
    return category["locations"].get(location_code)


def get_sub_location(state, category_code, location_code, sub_code):
    location = get_location(state, category_code, location_code)
    if not location:
        return None
    return location["sub_locations"].get(sub_code)


def find_location_anywhere(state, code):
    """
    Search every category/parent/sub-location in a state for a matching code,
    since players will usually just type a location name without knowing
    which category it lives under.

    Returns (category_code, location_code, sub_code_or_None, location_data)
    or None if nothing matches.
    """
    for category_code, category in LOCATIONS.get(state, {}).items():
        for location_code, location in category["locations"].items():
            if location_code == code:
                return category_code, location_code, None, location
            for sub_code, sub in location.get("sub_locations", {}).items():
                if sub_code == code:
                    return category_code, location_code, sub_code, sub
    return None


def find_overseas(code):
    return OVERSEAS_LOCATIONS.get(code)


# ---------------------------------------------------------------------------
# Role-gating helpers
# ---------------------------------------------------------------------------

def has_access(role_names, access):
    """
    True if the given role names satisfy an ACCESS spec:
    any one group (OR) where every role in the group is held (AND).
    access=None means ungated. Role matching is case-insensitive.
    Pass e.g. [r.name for r in member.roles].
    """
    if access is None:
        return True
    have = {r.casefold() for r in role_names}
    return any(all(r.casefold() in have for r in group) for group in access)


def guest_pass_needed(role_names, access):
    """
    For Governor's House locations: if the person only gets in through a guest
    role, returns the pass_type ("visitor" or "guest") the bot must confirm in
    the database (guest_passes) for THIS state. Returns None if entry doesn't
    depend on a guest role (e.g. they're a resident governor/deputy).
    """
    if access is None:
        return None
    have = {r.casefold() for r in role_names}
    pass_types = set()
    for group in access:
        if all(r.casefold() in have for r in group):
            guest_roles = [r for r in group if r in GUEST_PASS_ROLES]
            if not guest_roles:
                return None            # satisfied without a guest role
            pass_types.update(GUEST_PASS_ROLES[r] for r in guest_roles)
    return next(iter(pass_types)) if len(pass_types) == 1 else None


def can_enter(state, code, role_names):
    """Can someone holding role_names enter this location/sub-location in a state?"""
    hit = find_location_anywhere(state, code)
    if hit is None:
        return False
    return has_access(role_names, hit[3]["access"])


def can_fly_to(code, role_names):
    loc = find_overseas(code)
    return loc is not None and has_access(role_names, loc["access"])


def required_roles(state=None):
    """Every role name referenced by the gating (handy for creating/auditing Discord roles)."""
    roles = set()

    def collect(node):
        for group in node.get("access") or []:
            roles.update(group)

    states = [state] if state else STATES
    for s in states:
        for category in LOCATIONS[s].values():
            for loc in category["locations"].values():
                collect(loc)
                for sub in loc["sub_locations"].values():
                    collect(sub)
    if state is None:
        for loc in OVERSEAS_LOCATIONS.values():
            collect(loc)
    return sorted(roles)
