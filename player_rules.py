"""
player_rules.py

Pure helpers for the join / immigration flow (no Discord or database access,
so they're easy to test).
"""

from locations import STATES, STATE_CODES

MIN_NAME_WORDS = 2      # "Full Name" = at least first + last name
MIN_AGE = 1
MAX_AGE = 120
MAX_NAME_LENGTH = 32    # Discord's nickname limit — !name changes the player's server nickname

# Roles every newly named player receives (plus "<State> Indigene")
DEFAULT_NEW_ROLES = ("Illiterate", "Jobless", "Homeless", "Single")
GENDER_OPTIONS = ("Male", "Female")


def arrival_role(state):
    return f"{state} Arrival"


def indigene_role(state):
    return f"{state} Indigene"


def format_nin(number, state):
    """NIN + zero-padded number + state code, e.g. NIN0007DL / NIN0012FCT."""
    return f"NIN{number:04d}{STATE_CODES[state]}"


def parse_name_and_age(details):
    """
    Parse the text after the @player in `!name @player <Full Name> <age>`.
    Returns (full_name, age). Raises ValueError with a player-friendly message.
    """
    parts = details.split()
    if len(parts) < MIN_NAME_WORDS + 1:
        raise ValueError("Usage: `!name @player <Full Name> <age>` (full name needs at least two names).")

    age_text = parts[-1]
    if not age_text.isdigit():
        raise ValueError("The age must be a number at the end, e.g. `!name @player Ada Obi 25`.")
    age = int(age_text)
    if not MIN_AGE <= age <= MAX_AGE:
        raise ValueError(f"Age must be between {MIN_AGE} and {MAX_AGE}.")

    name_parts = parts[:-1]
    for word in name_parts:
        if not all(ch.isalpha() or ch in "-'." for ch in word):
            raise ValueError("Names can only contain letters, hyphens, apostrophes and full stops.")

    full_name = " ".join(name_parts)
    if len(full_name) > MAX_NAME_LENGTH:
        raise ValueError(f"The name is too long ({len(full_name)} characters). Discord allows at most {MAX_NAME_LENGTH}.")

    return full_name, age


def find_roles(guild_roles, names):
    """
    Look up roles by name (case-insensitive) among guild_roles.
    Returns (found_role_objects, missing_names). Never creates roles.
    """
    by_name = {r.name.casefold(): r for r in guild_roles}
    found, missing = [], []
    for name in names:
        role = by_name.get(name.casefold())
        if role:
            found.append(role)
        else:
            missing.append(name)
    return found, missing


def onboarding_actions(before_names, after_names, player):
    """
    Decide what to do when a member's roles change because of Discord's
    onboarding questions (which hand out "<State> Arrival" and Male/Female).

    before_names / after_names: role names before and after the change.
    player: None (no record yet) or a row/dict with current_state, gender,
            immigration_status.

    Returns a dict:
        create       (state, gender_or_None) if a player record should be created
        set_gender   gender to save on an existing record that has none
        remove_roles role names to take away again (destination/gender are locked
                     once chosen — members can re-answer onboarding questions later)
        add_roles    role names to put back
    """
    before = {n.casefold() for n in before_names}
    after = {n.casefold() for n in after_names}
    gained = after - before

    def held(names, pool):
        return [n for n in pool if n.casefold() in names]

    gained_states = [s for s in STATES if arrival_role(s).casefold() in gained]
    after_states = [s for s in STATES if arrival_role(s).casefold() in after]
    gained_genders = held(gained, GENDER_OPTIONS)
    after_genders = held(after, GENDER_OPTIONS)

    result = {"create": None, "set_gender": None, "remove_roles": [], "add_roles": []}

    if player is None:
        if not after_states:
            return result                     # gender alone doesn't start a journey
        state = gained_states[0] if gained_states else after_states[0]
        gender = (gained_genders or after_genders or [None])[0]
        result["create"] = (state, gender)
        result["remove_roles"] = [arrival_role(s) for s in after_states if s != state]
        return result

    # Existing player: the destination is locked once chosen.
    for s in gained_states:
        if s != player["current_state"] or player["immigration_status"] != "arrived":
            result["remove_roles"].append(arrival_role(s))

    if gained_genders:
        if not player["gender"]:
            result["set_gender"] = gained_genders[0]
        elif gained_genders[0] != player["gender"]:
            result["remove_roles"].append(gained_genders[0])
            result["add_roles"].append(player["gender"])

    return result


def find_arrival_terminal(text_channels, state):
    """
    Find a state's arrival-terminal automatically — no setup command needed.
    Channels are matched by name ("arrival-terminal"), then narrowed to the state by
    (1) the channel being visible to that state's "<State> Arrival" role,
    (2) otherwise the state's name appearing in its category or channel name.
    Returns the channel, or None if it can't tell which one belongs to the state.
    """
    wanted_role = arrival_role(state).casefold()
    candidates = [c for c in text_channels if "arrival-terminal" in c.name.casefold()]

    for channel in candidates:
        if any(getattr(target, "name", "").casefold() == wanted_role for target in channel.overwrites):
            return channel

    for channel in candidates:
        category = getattr(channel.category, "name", "")
        if state.casefold() in category.casefold() or state.casefold() in channel.name.casefold():
            return channel
    return None


def is_front_desk(channel_name):
    """True for the front-desk channel (an emoji/decoration prefix is fine)."""
    return "front-desk" in channel_name.casefold()


def is_immigration_office(channel_name):
    """True for the immigration-office channel itself (not its front-desk sub-channel)."""
    name = channel_name.casefold()
    return "immigration-office" in name and "front-desk" not in name
