"""
Meet Results Parser & Analysis Engine
Supports: PDF (HY-TEK Meet Manager format), HY-TEK .hy3/.cl2 text exports
Parses swimmer results, simulates placement, calculates points.
"""

import re
import io


# ── Scoring ────────────────────────────────────────────────────────────────────
INDIVIDUAL_SCORING = {
    1: 20, 2: 17, 3: 16, 4: 15, 5: 14, 6: 13, 7: 12, 8: 11,
    9: 9, 10: 7, 11: 6, 12: 5, 13: 4, 14: 3, 15: 2, 16: 1
}

RELAY_SCORING = {
    1: 40, 2: 34, 3: 32, 4: 30, 5: 28, 6: 26, 7: 24, 8: 22,
    9: 18, 10: 14, 11: 12, 12: 10, 13: 8, 14: 6, 15: 4, 16: 2
}


def get_points(place, relay=False):
    scoring = RELAY_SCORING if relay else INDIVIDUAL_SCORING
    return scoring.get(place, 0)


# ── Time utilities ─────────────────────────────────────────────────────────────

def parse_time_to_seconds(t):
    if t is None:
        return None
    t = str(t).strip().lstrip('J').replace('AUTO', '').replace('auto', '').strip()
    if not t or t in ('-', '--', 'DQ', 'NS', 'SCR', 'DNF'):
        return None
    try:
        if ':' in t:
            parts = t.split(':')
            return int(parts[0]) * 60 + float(parts[1])
        return float(t)
    except Exception:
        return None


def seconds_to_display(s):
    if s is None:
        return '--'
    m = int(s // 60)
    sec = s % 60
    if m > 0:
        return f"{m}:{sec:05.2f}"
    return f"{sec:.2f}"


# ── Event name normalization ───────────────────────────────────────────────────

# Maps short app names -> full stroke names used in parsed PDFs
STROKE_MAP = {
    'Free':   'Freestyle',
    'Back':   'Backstroke',
    'Breast': 'Breaststroke',
    'Fly':    'Butterfly',
    'IM':     'IM',
}

def normalize_event_name(event_str):
    """
    Normalize app event name to match parsed PDF/HY-TEK format.
    Handles both SCY ('100 Free' -> '100 Freestyle') and
    LCM/SCM ('100 Free' -> '100 Freestyle') formats.
    Also strips distance units like 'Yard' or 'Meter' from parsed events.
    """
    event_str = event_str.strip()
    parts = event_str.split(' ', 1)
    if len(parts) != 2:
        return event_str
    distance, stroke = parts[0], parts[1].strip()
    # Expand short stroke names
    full_stroke = STROKE_MAP.get(stroke, stroke)
    return f"{distance} {full_stroke}"


def normalize_parsed_event(event_str):
    """
    Normalize a parsed PDF event name by stripping 'Yard' or 'Meter' unit words.
    e.g. '100 Yard Freestyle' -> '100 Freestyle'
         '100 Meter Freestyle' -> '100 Freestyle'
         '200 IM' -> '200 IM'
    """
    event_str = event_str.strip()
    # Remove 'Yard', 'Yards', 'Meter', 'Meters' (case-insensitive)
    cleaned = re.sub(r'\b(Yard|Yards|Meter|Meters)\b\s*', '', event_str, flags=re.IGNORECASE).strip()
    # Collapse multiple spaces
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned


# ── PDF Parser ─────────────────────────────────────────────────────────────────

def parse_pdf_text(text):
    """
    Parse raw text extracted from a HY-TEK Meet Manager PDF.
    Supports both SCY ('Yard') and LCM/SCM ('Meter') meets.
    Returns list of result dicts.
    """
    lines = text.split('\n')
    results = []

    current_gender = None
    current_event = None
    current_section = None
    skip_event = False

    # Updated EVENT_RE to match both 'Yard' and 'Meter' meets
    EVENT_RE = re.compile(
        r'^\s+(Women|Men)\s+(\d+)\s+(Yard|Yards|Meter|Meters)\s+(.+)$',
        re.IGNORECASE
    )
    FINALS_RE  = re.compile(r'^\s+Finals\s*$', re.IGNORECASE)
    CONSOLS_RE = re.compile(r'^\s+Consols\s*$', re.IGNORECASE)
    CFINAL_RE  = re.compile(r'^\s+C\s*-?\s*Final\s*$', re.IGNORECASE)
    PRELIMS_RE = re.compile(r'^\s+Preliminaries\s*$', re.IGNORECASE)
    SWIMOFF_RE = re.compile(r'^\s+-\s+Swim-off\s*$', re.IGNORECASE)

    RESULT_RE = re.compile(
        r'^\s{3,}(\d{1,3}|--)\s+'
        r'([A-Z][a-zA-Z\-]+,\s+[A-Za-z][\w\s\-\.\']+?)\s{2,}'
        r'(\d{1,2})\s+'
        r'([A-Za-z][\w\s\-\/&\.\']+?-[A-Z]{2})\s+'
        r'((?:J?[\d]+:[\d]{2}\.[\d]+|J?[\d]+\.[\d]+)'
        r'(?:\s+(?:AUTO|auto))?'
        r'(?:\s+(?:J?[\d]+:[\d]{2}\.[\d]+|J?[\d]+\.[\d]+)(?:\s+(?:AUTO|auto))?)?)'
        r'\s*$'
    )

    SKIP_PATTERNS = [
        'http', 'Licensed', 'HY-TEK', 'Hosted by', '======',
        'Meet Qualifying', 'SDIF', 'Please note', 'download',
        '<< Back', 'Updated:', 'Results\n', 'Correction',
    ]

    for line in lines:
        if any(p in line for p in SKIP_PATTERNS):
            continue
        if re.match(r'^\s*\d+/\d+/\d+', line):
            continue
        if re.match(r'^\s*\d{1,2}:\d{2}\s+(AM|PM)', line):
            continue

        em = EVENT_RE.match(line)
        if em:
            gender    = em.group(1)
            distance  = em.group(2)
            # group(3) = 'Yard'/'Meter', group(4) = stroke
            stroke_raw = em.group(4).strip()

            if 'Swim-off' in stroke_raw or 'Relay' in stroke_raw:
                skip_event = True
                current_event = None
                continue

            skip_event = False
            current_gender = gender
            # Store WITHOUT the unit word so lookup is consistent
            current_event = f"{distance} {stroke_raw}"
            current_section = None
            continue

        if skip_event:
            continue

        if FINALS_RE.match(line) or CONSOLS_RE.match(line) or CFINAL_RE.match(line):
            if current_section != 'prelims':
                current_section = 'finals'
            continue
        if PRELIMS_RE.match(line):
            current_section = 'prelims'
            continue
        if SWIMOFF_RE.match(line):
            current_section = 'swimoff'
            continue

        if not current_event or current_section == 'swimoff':
            continue

        if re.match(r'^\s+[\d:\.]+\s+\([\d:\.]+\)', line):
            continue

        rm = RESULT_RE.match(line)
        if not rm:
            continue

        if re.search(r'\b(DQ|NS|SCR|DNF)\b', line):
            continue

        place_str = rm.group(1)
        name_raw  = rm.group(2).strip()
        age       = int(rm.group(3))
        team      = rm.group(4).strip()
        times_str = rm.group(5).strip()

        place = None if place_str == '--' else int(place_str)

        time_tokens = re.findall(r'J?[\d]+:[\d]{2}\.[\d]+|J?[\d]+\.[\d]+', times_str)

        prelim_time = None
        finals_time = None

        if current_section == 'finals':
            if len(time_tokens) >= 2:
                prelim_time = parse_time_to_seconds(time_tokens[0])
                finals_time = parse_time_to_seconds(time_tokens[1])
            elif len(time_tokens) == 1:
                finals_time = parse_time_to_seconds(time_tokens[0])
        elif current_section == 'prelims':
            if time_tokens:
                prelim_time = parse_time_to_seconds(time_tokens[-1])

        name_parts = name_raw.split(',', 1)
        if len(name_parts) == 2:
            first = name_parts[1].strip()
            last  = name_parts[0].strip()
            full_name = f"{first} {last}"
        else:
            full_name = name_raw

        results.append({
            'place':          place,
            'name':           full_name,
            'name_raw':       name_raw,
            'age':            age,
            'team':           team,
            'gender':         current_gender,
            'event':          current_event,
            'section':        current_section,
            'prelim_time':    prelim_time,
            'prelim_display': seconds_to_display(prelim_time),
            'finals_time':    finals_time,
            'finals_display': seconds_to_display(finals_time),
        })

    return results


def extract_pdf_text(file_bytes):
    import subprocess, tempfile, os
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
        f.write(file_bytes)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ['pdftotext', '-layout', tmp_path, '-'],
            capture_output=True, text=True, timeout=60
        )
        return result.stdout
    finally:
        os.unlink(tmp_path)


def parse_hytek_text(text):
    if re.search(r'(Women|Men)\s+\d+\s+(Yard|Meter)', text, re.IGNORECASE):
        return parse_pdf_text(text)

    results = []
    lines = text.split('\n')
    current_event = None
    current_gender = None

    for line in lines:
        em = re.match(r'^E\d+([WM])\s+(\d+)\s+(\w[\w\s]+)', line)
        if em:
            current_gender = 'Women' if em.group(1) == 'W' else 'Men'
            current_event  = f"{em.group(2)} {em.group(3).strip()}"
            continue

        rm = re.match(r'^D\d+\s+(\d+)\s+([\w,\s]+)\s+(\d{1,2})\s+([\w\s\-]+)\s+([\d:\.]+)', line)
        if rm and current_event:
            results.append({
                'place':          int(rm.group(1)),
                'name':           rm.group(2).strip(),
                'age':            int(rm.group(3)),
                'team':           rm.group(4).strip(),
                'gender':         current_gender,
                'event':          current_event,
                'section':        'finals',
                'prelim_time':    None,
                'prelim_display': '--',
                'finals_time':    parse_time_to_seconds(rm.group(5)),
                'finals_display': rm.group(5).strip(),
            })

    return results


# ── Meet Analysis ──────────────────────────────────────────────────────────────

def build_event_lookup(results):
    """
    Build lookup[gender][normalized_event] = { finals: [...], prelims: [...] }
    Event names are stored WITHOUT unit words (no 'Yard'/'Meter') for consistent matching.
    """
    lookup = {}
    for r in results:
        g = r['gender']
        # Normalize the parsed event name (strip Yard/Meter, expand stroke)
        e = normalize_event_name(normalize_parsed_event(r['event']))
        if g not in lookup:
            lookup[g] = {}
        if e not in lookup[g]:
            lookup[g][e] = {'finals': [], 'prelims': []}
        if r['section'] == 'finals' and r['finals_time']:
            lookup[g][e]['finals'].append(r)
        if r['prelim_time']:
            lookup[g][e]['prelims'].append(r)

    for g in lookup:
        for e in lookup[g]:
            lookup[g][e]['finals'].sort(key=lambda x: x['finals_time'])
            lookup[g][e]['prelims'].sort(key=lambda x: x['prelim_time'])

    return lookup


def simulate_placement(predicted_seconds, gender, event_name, lookup):
    """
    Simulate where a swimmer's predicted time would place in the meet field.

    Logic:
    - If prelims exist: use prelim times to determine seeding and A/B final cutlines
    - If no prelims (finals-only meet): use finals times directly for seeding
    - Project finals placement using the same pool of times
    """
    norm_event = normalize_event_name(event_name)
    event_data = lookup.get(gender, {}).get(norm_event, {'finals': [], 'prelims': []})

    prelim_times = sorted([r['prelim_time'] for r in event_data['prelims'] if r['prelim_time']])
    finals_times = sorted([r['finals_time'] for r in event_data['finals'] if r['finals_time']])

    # Use prelims if available, otherwise fall back to finals for seeding
    seed_times = prelim_times if prelim_times else finals_times
    total = len(seed_times)

    if total == 0:
        # Event not found in meet
        return {
            'prelim_seed': None,
            'total_prelim_field': 0,
            'makes_a_final': False,
            'makes_b_final': False,
            'projected_finals_place': None,
            'projected_points': 0,
            'cutline_a_final': '--',
            'cutline_b_final': '--',
            'gap_to_a_final_seconds': None,
            'gap_to_b_final_seconds': None,
            'gap_to_a_final_display': '--',
            'gap_to_b_final_display': '--',
            'event_in_meet': False,
        }

    # Seeding position
    prelim_seed = sum(1 for t in seed_times if t < predicted_seconds) + 1

    # Cutlines: 8th and 16th seed time
    cutline_a = seed_times[7]  if len(seed_times) >= 8  else seed_times[-1]
    cutline_b = seed_times[15] if len(seed_times) >= 16 else None

    gap_to_a = round(predicted_seconds - cutline_a, 2)
    gap_to_b = round(predicted_seconds - cutline_b, 2) if cutline_b else None

    makes_a = prelim_seed <= 8
    makes_b = not makes_a and prelim_seed <= 16

    # Project finals place using finals times if available, else seed times
    project_pool = finals_times if finals_times else seed_times

    projected_place  = None
    projected_points = 0

    if makes_a:
        # Compare against top 8 in finals pool
        a_pool = project_pool[:8] if len(project_pool) >= 8 else project_pool
        projected_place  = sum(1 for t in a_pool if t < predicted_seconds) + 1
        projected_points = get_points(projected_place)
    elif makes_b:
        # Compare against places 9-16 in finals pool
        b_pool = project_pool[8:16] if len(project_pool) >= 16 else project_pool[8:] if len(project_pool) > 8 else []
        if b_pool:
            projected_place  = sum(1 for t in b_pool if t < predicted_seconds) + 9
        else:
            projected_place = prelim_seed
        projected_points = get_points(projected_place)

    return {
        'prelim_seed':               prelim_seed,
        'total_prelim_field':        total,
        'makes_a_final':             makes_a,
        'makes_b_final':             makes_b,
        'projected_finals_place':    projected_place,
        'projected_points':          projected_points,
        'cutline_a_final':           seconds_to_display(cutline_a),
        'cutline_b_final':           seconds_to_display(cutline_b),
        'gap_to_a_final_seconds':    gap_to_a,
        'gap_to_b_final_seconds':    gap_to_b,
        'gap_to_a_final_display':    f"+{gap_to_a:.2f}s" if gap_to_a > 0 else f"{gap_to_a:.2f}s",
        'gap_to_b_final_display':    (f"+{gap_to_b:.2f}s" if gap_to_b > 0 else f"{gap_to_b:.2f}s") if gap_to_b is not None else '--',
        'event_in_meet':             True,
    }


# ── Lineup Optimizer ───────────────────────────────────────────────────────────

def optimize_lineup(swimmers, lookup, max_events_per_swimmer=3, max_entries_per_event=None):
    """
    Find event assignments that maximize total team projected points.
    Uses greedy algorithm sorted by projected points descending.
    """
    attending = [s for s in swimmers if s.get('attending', True)]

    options = []
    for swimmer in attending:
        gender = swimmer['gender']
        for event, pred_seconds in swimmer.get('predicted_times', {}).items():
            if pred_seconds is None:
                continue
            placement = simulate_placement(pred_seconds, gender, event, lookup)
            if not placement['event_in_meet']:
                continue
            options.append({
                'swimmer':           swimmer['name'],
                'gender':            gender,
                'event':             event,
                'predicted_seconds': pred_seconds,
                'projected_place':   placement['projected_finals_place'],
                'projected_points':  placement['projected_points'],
                'prelim_seed':       placement['prelim_seed'],
                'makes_a_final':     placement['makes_a_final'],
                'makes_b_final':     placement['makes_b_final'],
            })

    options.sort(key=lambda x: x['projected_points'], reverse=True)

    assignments    = {s['name']: [] for s in attending}
    event_assignments = {}
    total_points   = 0
    breakdown      = []

    for opt in options:
        swimmer  = opt['swimmer']
        event    = opt['event']

        if len(assignments[swimmer]) >= max_events_per_swimmer:
            continue

        event_key = f"{opt['gender']}_{event}"
        if max_entries_per_event and len(event_assignments.get(event_key, [])) >= max_entries_per_event:
            continue

        assignments[swimmer].append(event)
        event_assignments.setdefault(event_key, []).append(swimmer)
        total_points += opt['projected_points']
        breakdown.append(opt)

    return {
        'assignments':            assignments,
        'total_projected_points': total_points,
        'breakdown':              breakdown,
        'unassigned_events': [
            opt for opt in options
            if opt['event'] not in assignments.get(opt['swimmer'], [])
        ]
    }
