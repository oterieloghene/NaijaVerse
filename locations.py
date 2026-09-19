"""
locations.py

Hardcoded location hierarchy for the RP bot.

No role-based access control here on purpose — this is pure location DATA.
Movement/permission systems can be layered on top of this later.

Structure:
    LOCATIONS[state][category_code] = {
        "display_name": str,
        "locations": {
            location_code: {
                "name": str,
                "non_physical": bool,
                "voice_channel": bool,
                "flight_only": bool,
                "sub_locations": {
                    sub_code: {
                        "name": str,
                        "non_physical": bool,
                        "voice_channel": bool,
                    },
                    ...
                }
            },
            ...
        }
    }

OVERSEAS_LOCATIONS is separate — not tied to any state, flight-only.
"""

STATES = ["Lagos", "Delta", "Abuja"]


def _loc(name, non_physical=False, voice_channel=False, flight_only=False, sub_locations=None):
    return {
        "name": name,
        "non_physical": non_physical,
        "voice_channel": voice_channel,
        "flight_only": flight_only,
        "sub_locations": sub_locations or {},
    }


def _sub(name, non_physical=False, voice_channel=False):
    return {"name": name, "non_physical": non_physical, "voice_channel": voice_channel}


def _common_categories():
    """Categories identical across Lagos, Delta, and Abuja."""
    return {
        "border_entry": {
            "display_name": "Border & Entry",
            "locations": {
                "arrival-terminal": _loc("Arrival Terminal", non_physical=True),
                "airport": _loc("Airport", non_physical=True),
                "immigration-office": _loc("Immigration Office", sub_locations={
                    "refugee-camp": _sub("Refugee Camp"),
                    "travel-agency": _sub("Travel Agency"),
                    "front-desk": _sub("Front Desk"),
                    "parcel-pickup": _sub("Parcel Pickup"),
                    "chief-marshal-office": _sub("Chief Marshal Office"),
                }),
            },
        },
        "hotel_suites": {
            "display_name": "Hotel & Suites",
            "locations": {
                "hotel-reception": _loc("Hotel Reception"),
            },
        },
        "broadcasting_station": {
            "display_name": "Broadcasting Station",
            "locations": {
                "broadcasting-station": _loc("Broadcasting Station", sub_locations={
                    "production-room": _sub("Production Room"),
                    "media-manager": _sub("Media Manager"),
                }),
            },
        },
        "judiciary": {
            "display_name": "Judiciary",
            "locations": {
                "clerk-office": _loc("Clerk Office", sub_locations={
                    "lawyer-chambers": _sub("Lawyer Chambers"),
                    "judge-chambers": _sub("Judge Chambers"),
                    "court-room": _sub("Court Room", voice_channel=True),
                }),
            },
        },
        "state_government": {
            "display_name": "State Government",
            "locations": {
                "governor-office": _loc("Governor Office"),
                "deputy-governor-office": _loc("Deputy Governor Office"),
                "chief-of-staff": _loc("Chief of Staff"),
                "state-secretariat": _loc("State Secretariat"),
                "council": _loc("Council", sub_locations={
                    "ministry-of-finance": _sub("Ministry of Finance"),
                    "ministry-of-justice": _sub("Ministry of Justice"),
                    "ministry-of-agriculture": _sub("Ministry of Agriculture"),
                    "ministry-of-housing": _sub("Ministry of Housing"),
                    "ministry-of-education": _sub("Ministry of Education"),
                    "ministry-of-commerce": _sub("Ministry of Commerce"),
                    "ministry-of-power-and-water": _sub("Ministry of Power and Water"),
                    "ministry-of-petroleum": _sub("Ministry of Petroleum"),
                    # Literal channel name is "council" but this one is the voice channel
                    "council-voice": _sub("Council", voice_channel=True),
                }),
                "city-hall": _loc("City Hall"),
            },
        },
        "governors_house": {
            "display_name": "Governor's House",
            "locations": {
                "governor-penthouse": _loc("Governor Penthouse", sub_locations={
                    "deputy-governor-residence": _sub("Deputy Governor Residence"),
                    "governor-guesthouse": _sub("Governor Guesthouse"),
                }),
            },
        },
        "bank_plc": {
            "display_name": "Bank Plc",
            "locations": {
                "banking-hall": _loc("Banking Hall", sub_locations={
                    "atm": _sub("ATM"),
                    "banking-office": _sub("Banking Office"),
                    "deposit": _sub("Deposit"),
                    "auditor": _sub("Auditor"),
                    "bank-manager": _sub("Bank Manager"),
                    "executive-director": _sub("Executive Director"),
                    "transaction-log": _sub("Transaction Log", non_physical=True),
                    "treasury": _sub("Treasury", non_physical=True),
                }),
            },
        },
        "police_department": {
            "display_name": "Police Department",
            "locations": {
                "police-station": _loc("Police Station", sub_locations={
                    "officers-office": _sub("Officers Office"),
                    "investigation-room": _sub("Investigation Room"),
                    "holding-cell": _sub("Holding Cell"),
                    "patrol": _sub("Patrol"),
                    "forensics": _sub("Forensics"),
                    "armoury": _sub("Armoury"),
                    "dcp-office": _sub("DCP Office"),
                    "cp-office": _sub("CP Office"),
                    "police-radio": _sub("Police Radio", non_physical=True),
                }),
            },
        },
        "property_and_development": {
            "display_name": "Property and Development Department",
            "locations": {
                "rental-desk": _loc("Rental Desk", sub_locations={
                    "staff-office": _sub("Staff Office"),
                }),
            },
        },
        "electricity_water": {
            "display_name": "Electricity and Water Distribution Company",
            "locations": {
                "help-desk": _loc("Help Desk", sub_locations={
                    "technician-office": _sub("Technician Office"),
                    "operations": _sub("Operations"),
                    "billing": _sub("Billing", non_physical=True),
                    "fault-report": _sub("Fault Report", non_physical=True),
                }),
            },
        },
        "low_cost_housing": {
            "display_name": "Low-Cost Housing",
            "locations": {
                "line-houses": _loc("Line Houses"),
                "bed-sitter": _loc("Bed-Sitter"),
            },
        },
        "mid_class_residential": {
            "display_name": "Mid-Class Residential",
            "locations": {
                "mini-flat": _loc("Mini Flat"),
                "two-bedroom-flat": _loc("Two Bedroom Flat"),
                "three-bedroom-flat": _loc("Three Bedroom Flat"),
            },
        },
        "high_class_residential": {
            "display_name": "High-Class Residential",
            "locations": {
                "private-estate": _loc("Private Estate"),
                "luxury-duplex": _loc("Luxury Duplex"),
                "penthouse": _loc("Penthouse"),
            },
        },
        "career_high_school": {
            "display_name": "Career High School",
            "locations": {
                "school-building": _loc("School Building"),
            },
        },
        "market_district": {
            "display_name": "Market District",
            "locations": {
                "market-district": _loc("Market District", sub_locations={
                    "main-market": _sub("Main Market"),
                    "abattoir": _sub("Abattoir"),
                }),
            },
        },
        "automotive_district": {
            "display_name": "Automotive District",
            "locations": {
                "automotive-district": _loc("Automotive District", sub_locations={
                    "nnpc-fuel-station": _sub("NNPC Fuel Station"),
                    "auto-repair": _sub("Auto Repair"),
                    "dealership": _sub("Dealership"),
                    "driving-school": _sub("Driving School"),
                }),
            },
        },
        "transport_district": {
            "display_name": "Transport District",
            "locations": {
                "transport-district": _loc("Transport District", sub_locations={
                    "bus-park": _sub("Bus Park"),
                    "taxi-company": _sub("Taxi Company"),
                }),
            },
        },
        "commercial_district": {
            "display_name": "Commercial District",
            "locations": {
                "commercial-district": _loc("Commercial District", sub_locations={
                    "mall": _sub("Mall"),
                    "club-house": _sub("Club House"),
                    "resort": _sub("Resort"),
                }),
            },
        },
        "industrial_district": {
            "display_name": "Industrial District",
            "locations": {
                "industrial-district": _loc("Industrial District", sub_locations={
                    "depot": _sub("Depot"),
                    "industrial-goods": _sub("Industrial Goods"),
                    "food-beverages-industry": _sub("Food & Beverages Industry"),
                    "refinery": _sub("Refinery"),
                }),
            },
        },
        "rural_area": {
            "display_name": "Rural Area",
            "locations": {
                # sub_locations filled in per-state below (farmland/ranch/river vary by state)
                "rural-district": _loc("Rural District", sub_locations={}),
            },
        },
    }


def _hospital_category(display_name):
    return {
        "display_name": display_name,
        "locations": {
            "hospital-lobby": _loc("Hospital Lobby", sub_locations={
                "hospital-reception": _sub("Hospital Reception"),
                "nursing-station": _sub("Nursing Station"),
                "emergency": _sub("Emergency"),
                "pharmacy-and-laboratory": _sub("Pharmacy and Laboratory"),
                "consultation": _sub("Consultation"),
                "dentistry": _sub("Dentistry"),
                "female-wards": _sub("Female Wards"),
                "male-wards": _sub("Male Wards"),
                "surgical-theatre": _sub("Surgical Theatre"),
                "dmd-office": _sub("DMD Office"),
                "cmd-office": _sub("CMD Office"),
            }),
        },
    }


# ---------------------------------------------------------------------------
# Build LOCATIONS for all three states from the shared template
# ---------------------------------------------------------------------------

LOCATIONS = {}

for _state in STATES:
    _categories = _common_categories()

    if _state == "Abuja":
        _categories["hospital"] = _hospital_category("Abuja Specialist Hospital")
    else:
        _categories["hospital"] = _hospital_category(f"{_state} General Hospital")

    LOCATIONS[_state] = _categories

# --- Rural area sub-locations vary by state ---
# farmland: Delta and Abuja only | ranch: Delta and Abuja only | river: Delta and Lagos only
LOCATIONS["Delta"]["rural_area"]["locations"]["rural-district"]["sub_locations"] = {
    "farmland": _sub("Farmland"),
    "ranch": _sub("Ranch"),
    "river": _sub("River"),
}
LOCATIONS["Abuja"]["rural_area"]["locations"]["rural-district"]["sub_locations"] = {
    "farmland": _sub("Farmland"),
    "ranch": _sub("Ranch"),
}
LOCATIONS["Lagos"]["rural_area"]["locations"]["rural-district"]["sub_locations"] = {
    "river": _sub("River"),
}

# ---------------------------------------------------------------------------
# Abuja-only categories
# ---------------------------------------------------------------------------

LOCATIONS["Abuja"]["aso_rock"] = {
    "display_name": "Aso Rock",
    "locations": {
        "president-office": _loc("President Office"),
        "vice-president-office": _loc("Vice President Office"),
        "chief-of-staff": _loc("Chief of Staff"),
        "president-villa": _loc("President Villa", sub_locations={
            "vice-president-residence": _sub("Vice President Residence"),
            "villa-guesthouse": _sub("Villa Guesthouse"),
        }),
    },
}

LOCATIONS["Abuja"]["central_bank"] = {
    "display_name": "Central Bank of Nigeria",
    "locations": {
        "cbn-lobby": _loc("CBN Lobby", sub_locations={
            "vault": _sub("Vault"),
            "cbn-deputy": _sub("CBN Deputy"),
            "cbn-governor": _sub("CBN Governor"),
            "national-treasury": _sub("National Treasury", non_physical=True),
        }),
    },
}

# ---------------------------------------------------------------------------
# State-specific universities (one each, different name/sub-locations)
# ---------------------------------------------------------------------------

LOCATIONS["Delta"]["university"] = {
    "display_name": "Delta State University",
    "locations": {
        "administrative-block": _loc("Administrative Block", sub_locations={
            "school-of-law": _sub("School of Law"),
            "school-of-nursing": _sub("School of Nursing"),
            "school-of-admin": _sub("School of Admin"),
            "campus-residence": _sub("Campus Residence"),
        }),
    },
}

LOCATIONS["Lagos"]["university"] = {
    "display_name": "Lagos Defence Academy",
    "locations": {
        "administrative-block": _loc("Administrative Block", sub_locations={
            "training-ground": _sub("Training Ground"),
            "lecture-area": _sub("Lecture Area"),
            "campus-residence": _sub("Campus Residence"),
        }),
    },
}

LOCATIONS["Abuja"]["university"] = {
    "display_name": "Abuja School of Banking and Medicine",
    "locations": {
        "administrative-block": _loc("Administrative Block", sub_locations={
            "school-of-medicine": _sub("School of Medicine"),
            "school-of-banking": _sub("School of Banking"),
            "campus-residence": _sub("Campus Residence"),
        }),
    },
}

# ---------------------------------------------------------------------------
# State-specific business locations
# ---------------------------------------------------------------------------

LOCATIONS["Delta"]["state_specific_business"] = {
    "display_name": "State-Specific Business Locations",
    "locations": {
        "oil-well": _loc("Oil Well"),
    },
}

LOCATIONS["Lagos"]["state_specific_business"] = {
    "display_name": "State-Specific Business Locations",
    "locations": {
        "sea-port": _loc("Sea Port"),
    },
}

LOCATIONS["Abuja"]["state_specific_business"] = {
    "display_name": "State-Specific Business Locations",
    "locations": {
        "mining-ground": _loc("Mining Ground"),
        "kanji-dam": _loc("Kanji Dam"),
    },
}

# ---------------------------------------------------------------------------
# Overseas — flight-only, not tied to any state
# ---------------------------------------------------------------------------

OVERSEAS_LOCATIONS = {
    "dubai": _loc("Dubai", flight_only=True),
    "seychelles": _loc("Seychelles", flight_only=True),
    "maldive": _loc("Maldive", flight_only=True),
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
