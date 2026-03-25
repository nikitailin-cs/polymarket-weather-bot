"""
Fuseaux IANA pour stations météo (ICAO / WMO) — aligné sur ``CITY_STATION`` du contract_parser.

Utilisé par ``generate_markets`` pour remplir ``timezone`` dans ``active_markets.json``
(entrée du moteur temporel GFS bot1).
"""
from __future__ import annotations

from typing import Optional

# ICAO US + Canada majeurs + aéroports fréquents Polymarket
_ICAO_IANA: dict[str, str] = {
    "KNYC": "America/New_York",
    "KJFK": "America/New_York",
    "KLGA": "America/New_York",
    "KEWR": "America/New_York",
    "KLAX": "America/Los_Angeles",
    "KSFO": "America/Los_Angeles",
    "KORD": "America/Chicago",
    "KMIA": "America/New_York",
    "KIAH": "America/Chicago",
    "KPHX": "America/Phoenix",
    "KATL": "America/New_York",
    "KDFW": "America/Chicago",
    "KSEA": "America/Los_Angeles",
    "KDEN": "America/Denver",
    "KBOS": "America/New_York",
    "KLAS": "America/Los_Angeles",
    "KMSP": "America/Chicago",
    "CYYZ": "America/Toronto",
    "RJTT": "Asia/Tokyo",
    "RJAA": "Asia/Tokyo",
    "LFPG": "Europe/Paris",
    "EGLL": "Europe/London",
    "EDDF": "Europe/Berlin",
}

# WMO (5 chiffres) → IANA (ville représentative)
_WMO_IANA: dict[str, str] = {
    "03772": "Europe/London",
    "07156": "Europe/Paris",
    "10384": "Europe/Berlin",
    "08221": "Europe/Madrid",
    "16240": "Europe/Rome",
    "16080": "Europe/Rome",
    "06240": "Europe/Amsterdam",
    "06451": "Europe/Brussels",
    "11035": "Europe/Vienna",
    "06670": "Europe/Zurich",
    "10866": "Europe/Berlin",
    "02485": "Europe/Stockholm",
    "01492": "Europe/Oslo",
    "12375": "Europe/Warsaw",
    "11518": "Europe/Prague",
    "12843": "Europe/Budapest",
    "08535": "Europe/Lisbon",
    "16716": "Europe/Athens",
    "17060": "Europe/Istanbul",
    "17130": "Europe/Istanbul",
    "03969": "Europe/Dublin",
    "06180": "Europe/Copenhagen",
    "02978": "Europe/Helsinki",
    "15420": "Europe/Bucharest",
    "47662": "Asia/Tokyo",
    "47772": "Asia/Tokyo",
    "54511": "Asia/Shanghai",
    "58367": "Asia/Shanghai",
    "59493": "Asia/Shanghai",
    "57494": "Asia/Shanghai",
    "57516": "Asia/Shanghai",
    "57036": "Asia/Shanghai",
    "58238": "Asia/Shanghai",
    "54527": "Asia/Shanghai",
    "56294": "Asia/Shanghai",
    "59287": "Asia/Shanghai",
    "47108": "Asia/Seoul",
    "48698": "Asia/Singapore",
    "43003": "Asia/Kolkata",
    "42182": "Asia/Kolkata",
    "42279": "Asia/Kolkata",
    "48455": "Asia/Bangkok",
    "96749": "Asia/Jakarta",
    "41194": "Asia/Dubai",
    "41780": "Asia/Karachi",
    "45007": "Asia/Hong_Kong",
    "46692": "Asia/Taipei",
    "48650": "Asia/Kuala_Lumpur",
    "98429": "Asia/Manila",
    "94767": "Australia/Sydney",
    "94866": "Australia/Melbourne",
    "87582": "America/Argentina/Buenos_Aires",
    "83779": "America/Sao_Paulo",
    "83743": "America/Sao_Paulo",
    "76679": "America/Mexico_City",
    "62366": "Africa/Cairo",
    "65201": "Africa/Lagos",
    "63741": "Africa/Nairobi",
    "68816": "Africa/Johannesburg",
    "85574": "America/Santiago",
    "84628": "America/Lima",
    "80222": "America/Bogota",
}


def resolve_iana_timezone(station_id: str) -> Optional[str]:
    """
    Retourne un fuseau IANA pour un code station (ICAO 4 lettres ou WMO 5 chiffres), ou None.
    """
    sid = (station_id or "").strip().upper()
    if not sid:
        return None
    if sid in _ICAO_IANA:
        return _ICAO_IANA[sid]
    digits = "".join(ch for ch in sid if ch.isdigit())
    if len(digits) >= 1:
        wmo = digits.zfill(5)[-5:] if len(digits) >= 5 else digits.zfill(5)
        if wmo in _WMO_IANA:
            return _WMO_IANA[wmo]
    return None
