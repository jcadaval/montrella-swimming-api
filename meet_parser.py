"""
Meet Results Parser & Analysis Engine
Supports: PDF (HY-TEK Meet Manager format), HY-TEK .hy3/.cl2 text exports
Parses swimmer results, simulates placement, calculates points.
"""

import re
import io


# ── Scoring ────────────────────────────────────────────────────────────────────
# USA Swimming standard championship scoring
# A Final: places 1-8, B Final (Consols): places 9-16, C Final: no points
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
    """Convert '4:32.19', '22.15', 'J23.45' etc. to float seconds."""
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
    """Convert seconds float to '4:32.19' or '22.15' display string."""
    if s is None:
        return '--'
    m = int(s // 60)
    sec = s % 60
    if m > 0:
        return f"{m}:{sec:05.2f}"
    return f"{sec:.2f}"


# ── PDF Parser ─────────────────────────────────────────────────────────────────

def parse_pdf_text(text):
    """
    Parse raw text extracted from a HY-TEK Meet Manager PDF.
    Returns list of result dicts.
    """
    lines = text.split('\n')
    results = []

    current_gender = None
    current_event = None
    current_section = None   # 'finals' | 'prelims' | 'swimoff'
    skip_event = False

    # Patterns
    EVENT_RE = re.compile(r'^\s+(Women|Men)\s+(\d+)\s+Yard\s+(.+)$', re.IGNORECASE)
    FINALS_RE = re.compile(r'^\s+Finals\s*$', re.IGNORECASE)
    CONSOLS_RE = re.compile(r'^\s+Consols\s*$', re.IGNORECASE)
    CFINAL_RE = re.compile(r'^\s+C\s*-?\s*Final\s*$', re.IGNORECASE)
    PRELIMS_RE = re.compile(r'^\s+Preliminaries\s*$', re.IGNORECASE)
    SWIMOFF_RE = re.compile(r'^\s+-\s+Swim-off\s*$', re.IGNORECASE)

    # Result row: place, Last First, age, team, then time(s)
    RESULT_RE = re.compile(
        r'^\s{3,}(\d{1,3}|--)\s+'           # place
        r'([A-Z][a-zA-Z\-]+,\s+[A-Za-z][\w\s\-\.\']+?)\s{2,}'  # name
        r'(\d{1,2})\s+'                      # age
        r'([A-Za-z][\w\s\-\/&\.\']+?-[A-Z]{2})\s+'  # team
        r'((?:J?[\d]+:[\d]{2}\.[\d]+|J?[\d]+\.[\d]+)'  # first time
        r'(?:\s+(?:AUTO|auto))?'
        r'(?:\s+(?:J?[\d]+:[\d]{2}\.[\d]+|J?[\d]+\.[\d]+)(?:\s+(?:AUTO|auto))?)?)'  # optional second time
        r'\s*$'
    )

    SKIP_PATTERNS = [
        'http', 'Licensed', 'HY-TEK', 'Hosted by', '======',
        'Meet Qualifying', 'SDIF', 'Please note', 'download',
        '<< Back', 'Updated:', 'Results\n', 'Correction',
    ]

    for line in lines:
        # Skip junk
        if any(p in line for p in SKIP_PATTERNS):
            continue
        if re.match(r'^\s*\d+/\d+/\d+', line):  # date header
            continue
        if re.match(r'^\s*\d{1,2}:\d{2}\s+(AM|PM)', line):  # time header
            continue

        # Event header
        em = EVENT_RE.match(line)
        if em:
            gender = em.group(1)
            distance = em.group(2)
            stroke_raw = em.group(3).strip()

            if 'Swim-off' in stroke_raw or 'Relay' in stroke_raw:
                skip_event = True
                current_event = None
                continue

            skip_event = False
            current_gender = gender
            current_event = f"{distance} {stroke_raw}"
            current_section = None
            continue

        if skip_event:
            continue

        # Section markers
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

        # Skip split rows (lines that are just split times, no name)
        if re.match(r'^\s+[\d:\.]+\s+\([\d:\.]+\)', line):
            continue

        # Try result row
        rm = RESULT_RE.match(line)
        if not rm:
            continue

        # Skip disqualified/scratched
        if re.search(r'\b(DQ|NS|SCR|DNF)\b', line):
            continue

        place_str = rm.group(1)
        name_raw = rm.group(2).strip()
        age = int(rm.group(3))
        team = rm.group(4).strip()
        times_str = rm.group(5).strip()

        place = None if place_str == '--' else int(place_str)

        # Extract all time tokens from the times string
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

        # Parse name: "Last, First" -> "First Last"
        name_parts = name_raw.split(',', 1)
        if len(name_parts) == 2:
            first = name_parts[1].strip()
            last = name_parts[0].strip()
            full_name = f"{first} {last}"
        else:
            full_name = name_raw

        results.append({
            'place': place,
            'name': full_name,
            'name_raw': name_raw,
            'age': age,
            'team': team,
            'gender': current_gender,
            'event': current_event,
            'section': current_section,
            'prelim_time': prelim_time,
            'prelim_display': seconds_to_display(prelim_time),
            'finals_time': finals_time,
            'finals_display': seconds_to_display(finals_time),
        })

    return results


def extract_pdf_text(file_bytes):
    """Extract text from PDF bytes using pdftotext."""
    import subprocess
    import tempfile
    import os

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
    """
    Parse HY-TEK .hy3 / .cl2 text export format.
    These are fixed-width formats — handle common export style.
    Falls back to PDF-style parsing if format looks like PDF text.
    """
    # If it looks like a PDF text export (has 'Yard' event headers), use PDF parser
    if re.search(r'(Women|Men)\s+\d+\s+Yard', text):
        return parse_pdf_text(text)

    # HY-TEK CL2 format parsing
    results = []
    lines = text.split('\n')
    current_event = None
    current_gender = None

    for line in lines:
        # Event line: "E0001W  50 Free ..."
        em = re.match(r'^E\d+([WM])\s+(\d+)\s+(\w[\w\s]+)', line)
        if em:
            current_gender = 'Women' if em.group(1) == 'W' else 'Men'
            current_event = f"{em.group(2)} {em.group(3).strip()}"
            continue

        # Result line
        rm = re.match(r'^D\d+\s+(\d+)\s+([\w,\s]+)\s+(\d{1,2})\s+([\w\s\-]+)\s+([\d:\.]+)', line)
        if rm and current_event:
            results.append({
                'place': int(rm.group(1)),
                'name': rm.group(2).strip(),
                'age': int(rm.group(3)),
                'team': rm.group(4).strip(),
                'gender': current_gender,
                'event': current_event,
                'section': 'finals',
                'prelim_time': None,
                'prelim_display': '--',
                'finals_time': parse_time_to_seconds(rm.group(5)),
                'finals_display': rm.group(5).strip(),
            })

    return results


# ── Meet Analysis ──────────────────────────────────────────────────────────────

def build_event_lookup(results):
    """
    Build a lookup structure:
    lookup[gender][event] = {
        'finals': [sorted result rows by finals_time],
        'prelims': [sorted result rows by prelim_time],
    }
    """
    lookup = {}
    for r in results:
        g = r['gender']
        e = r['event']
        if g not in lookup:
            lookup[g] = {}
        if e not in lookup[g]:
            lookup[g][e] = {'finals': [], 'prelims': []}
        if r['section'] == 'finals' and r['finals_time']:
            lookup[g][e]['finals'].append(r)
        if r['prelim_time']:
            lookup[g][e]['prelims'].append(r)

    # Sort each list by time ascending
    for g in lookup:
        for e in lookup[g]:
            lookup[g][e]['finals'].sort(key=lambda x: x['finals_time'])
            lookup[g][e]['prelims'].sort(key=lambda x: x['prelim_time'])

    return lookup


def normalize_event_name(event_str):
    """
    Normalize event names to match parsed format.
    e.g. '100 Free' -> '100 Freestyle', '200 IM' -> '200 IM'
    """
    event_str = event_str.strip()
    replacements = {
        'Free': 'Freestyle',
        'Back': 'Backstroke',
        'Breast': 'Breaststroke',
        'Fly': 'Butterfly',
    }
    parts = event_str.split(' ', 1)
    if len(parts) == 2:
        distance = parts[0]
        stroke = parts[1]
        for short, full in replacements.items():
            if stroke == short:
                return f"{distance} {full}"
    return event_str


def simulate_placement(predicted_seconds, gender, event_name, lookup):
    """
    Given a swimmer's predicted time, determine where they'd place
    in the provided meet field.

    Returns dict with:
    - prelim_seed: rank in prelims
    - total_prelim_field: how many in the field
    - makes_a_final: bool
    - makes_b_final: bool
    - projected_finals_place: int or None
    - projected_points: int
    - cutline_a: time needed to make A final (8th place prelim time)
    - cutline_b: time needed to make B final (16th place prelim time)
    - gap_to_a_final: seconds away from making A final (negative = already in)
    - gap_to_b_final: seconds away from making B final
    """
    norm_event = normalize_event_name(event_name)
    event_data = lookup.get(gender, {}).get(norm_event, {'finals': [], 'prelims': []})

    prelim_times = sorted([r['prelim_time'] for r in event_data['prelims'] if r['prelim_time']])
    finals_rows = event_data['finals']
    a_final_times = sorted([r['finals_time'] for r in finals_rows if r['finals_time'] and r['place'] and r['place'] <= 8])
    b_final_times = sorted([r['finals_time'] for r in finals_rows if r['finals_time'] and r['place'] and 9 <= r['place'] <= 16])

    total = len(prelim_times)
    prelim_seed = sum(1 for t in prelim_times if t < predicted_seconds) + 1

    cutline_a = prelim_times[7] if len(prelim_times) >= 8 else None
    cutline_b = prelim_times[15] if len(prelim_times) >= 16 else None

    gap_to_a = (predicted_seconds - cutline_a) if cutline_a else None
    gap_to_b = (predicted_seconds - cutline_b) if cutline_b else None

    makes_a = prelim_seed <= 8
    makes_b = 9 <= prelim_seed <= 16

    projected_place = None
    projected_points = 0

    if makes_a and a_final_times:
        projected_place = sum(1 for t in a_final_times if t < predicted_seconds) + 1
        projected_points = get_points(projected_place)
    elif makes_b and b_final_times:
        projected_place = sum(1 for t in b_final_times if t < predicted_seconds) + 9
        projected_points = get_points(projected_place)

    return {
        'prelim_seed': prelim_seed,
        'total_prelim_field': total,
        'makes_a_final': makes_a,
        'makes_b_final': makes_b,
        'projected_finals_place': projected_place,
        'projected_points': projected_points,
        'cutline_a_final': seconds_to_display(cutline_a),
        'cutline_b_final': seconds_to_display(cutline_b),
        'gap_to_a_final_seconds': round(gap_to_a, 2) if gap_to_a is not None else None,
        'gap_to_b_final_seconds': round(gap_to_b, 2) if gap_to_b is not None else None,
        'gap_to_a_final_display': f"+{gap_to_a:.2f}s" if gap_to_a and gap_to_a > 0 else (f"{gap_to_a:.2f}s" if gap_to_a else '--'),
        'gap_to_b_final_display': f"+{gap_to_b:.2f}s" if gap_to_b and gap_to_b > 0 else (f"{gap_to_b:.2f}s" if gap_to_b else '--'),
        'event_in_meet': total > 0,
    }


# ── Lineup Optimizer ───────────────────────────────────────────────────────────

def optimize_lineup(swimmers, lookup, max_events_per_swimmer=3, max_entries_per_event=None):
    """
    Given a list of swimmers with their predicted times and availability,
    find the event assignments that maximize total team points.

    swimmers: list of {
        'name': str,
        'gender': 'Women'|'Men',
        'predicted_times': {'100 Freestyle': 52.3, ...},
        'attending': True
    }

    Returns:
    - assignments: {swimmer_name: [event, ...]}
    - total_projected_points: int
    - event_breakdown: [{event, swimmer, projected_place, projected_points}, ...]
    """
    attending = [s for s in swimmers if s.get('attending', True)]

    # Score every (swimmer, event) combination
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
                'swimmer': swimmer['name'],
                'gender': gender,
                'event': event,
                'predicted_seconds': pred_seconds,
                'projected_place': placement['projected_finals_place'],
                'projected_points': placement['projected_points'],
                'prelim_seed': placement['prelim_seed'],
                'makes_a_final': placement['makes_a_final'],
                'makes_b_final': placement['makes_b_final'],
            })

    # Sort by projected points descending (greedy optimization)
    options.sort(key=lambda x: x['projected_points'], reverse=True)

    assignments = {s['name']: [] for s in attending}
    event_assignments = {}  # event -> list of assigned swimmers
    total_points = 0
    breakdown = []

    for opt in options:
        swimmer = opt['swimmer']
        event = opt['event']

        # Check swimmer event limit
        if len(assignments[swimmer]) >= max_events_per_swimmer:
            continue

        # Check event entry limit
        event_key = f"{opt['gender']}_{event}"
        if max_entries_per_event and len(event_assignments.get(event_key, [])) >= max_entries_per_event:
            continue

        assignments[swimmer].append(event)
        event_assignments.setdefault(event_key, []).append(swimmer)
        total_points += opt['projected_points']
        breakdown.append(opt)

    return {
        'assignments': assignments,
        'total_projected_points': total_points,
        'breakdown': breakdown,
        'unassigned_events': [
            opt for opt in options
            if opt['event'] not in assignments.get(opt['swimmer'], [])
        ]
    }
