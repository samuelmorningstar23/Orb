"""Manually evidenced local surveillance, isolation and susceptibility records.

Nothing here infers infection, interprets MICs, alters isolation care orders,
recommends antibiotics, transmits a report or invents a denominator policy.
"""
from collections import defaultdict
from datetime import date
import json

from database import db_session
from db_backend import DBIntegrityError
from services import local_operations_common as c
from services.hospital_time import hospital_today

READ = 'infection_surveillance.read'
WRITE = 'infection_surveillance.write'
MANAGE = 'infection_surveillance.manage'
TABLES = {'definition': 'infection_definitions', 'window': 'infection_windows',
          'case': 'infection_cases', 'exposure': 'infection_exposure_days',
          'susceptibility': 'infection_susceptibility', 'isolation': 'infection_isolation_records'}
NOTICE = 'Manual local surveillance only. Case definitions, population, counting policy and results are entered against hospital evidence. No treatment recommendation or external report is generated.'
MAX_REPORT_ROWS = 100000


def _day(value, field):
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise c.OperationsError(f'{field} needs an ISO calendar date.') from None
    if parsed.isoformat() != value:
        raise c.OperationsError(f'{field} needs an ISO calendar date.')
    return value


def _get(db, kind, ident, actor):
    row = c.scoped_on(db, TABLES[kind], ident, actor)
    if kind in ('case', 'exposure', 'susceptibility'):
        _window(db, row['window_id'], actor)
    return row


def _window(db, ident, actor, *, writable=False):
    row = c.scoped_on(db, TABLES['window'], ident, actor)
    c.scoped_on(db, TABLES['definition'], row['definition_id'], actor)
    # Never publish an aggregate from an incomplete, ward-filtered population.
    # A transfer can invalidate a previously readable window for a scoped user.
    visible, params = c.patient_filter(actor)
    for table in ('infection_cases', 'infection_exposure_days', 'infection_susceptibility'):
        if db.execute(f'SELECT 1 FROM {table} WHERE window_id=? AND NOT {visible} LIMIT 1', (ident, *params)).fetchone():
            raise c.OperationsError('Observation window not found.', 'not_found', 404)
    if writable and row['status'] != 'open':
        raise c.OperationsError('Reopen the finalized window with a reason before recording a correction.', 'window_finalized', 409)
    return row


def _event(db, actor, kind, row, action, note, request_meta=None):
    window_id = row['id'] if kind == 'window' else row.get('window_id')
    db.execute('INSERT INTO infection_events(id,record_kind,record_id,window_id,patient_id,action,note,actor_id,occurred_at,version) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (c.new_id('ICE'), kind, row['id'], window_id, row.get('patient_id'), action, note, actor['id'], c.now(), row.get('version', 1)))
    c.audit_on(db, actor, 'infection_surveillance', row['id'], action, patient_id=row.get('patient_id'), meta=request_meta)


def _within(window, observed):
    if observed > hospital_today().isoformat():
        raise c.OperationsError("An observed day cannot be in the hospital calendar's future.")
    if not window['start_date'] <= observed <= window['end_date']:
        raise c.OperationsError('The observation date must be within the inclusive window dates.')


def _payload(kind, data):
    if kind not in TABLES:
        raise c.OperationsError('Unknown record type.')
    if kind == 'definition':
        result = {field: c.text(data.get(field), label, maximum) for field, label, maximum in (
            ('code', 'Definition code', 64), ('name', 'Definition name', 200),
            ('case_definition', 'Hospital case definition', 10000), ('counting_policy', 'Counting / episode policy', 4000),
            ('evidence_reference', 'Approved definition reference', 500))}
        result['code'] = result['code'].upper()
        result['denominator_unit'] = c.choice(data.get('denominator_unit'), ('patient_days', 'device_days'), 'denominator unit')
        result['device_label'] = c.text(data.get('device_label'), 'Defined device type', 200, result['denominator_unit'] == 'device_days')
        return result
    if kind == 'window':
        result = {field: c.text(data.get(field), label, maximum) for field, label, maximum in (
            ('definition_id', 'Definition', 100), ('label', 'Window label', 200),
            ('population_description', 'Explicit surveillance population', 4000), ('source_reference', 'Population / denominator source reference', 500))}
        result['start_date'], result['end_date'] = _day(data.get('start_date'), 'Start date'), _day(data.get('end_date'), 'End date')
        if result['end_date'] < result['start_date'] or (date.fromisoformat(result['end_date']) - date.fromisoformat(result['start_date'])).days > 3660:
            raise c.OperationsError('Choose an ordered observation window of no more than 3661 days.')
        result['rate_multiplier'] = c.number(data.get('rate_multiplier'), 'Hospital-selected rate multiplier', 1, 1000000)
        result['minimum_cell'] = c.number(data.get('minimum_cell'), 'Hospital-selected minimum cell', 1, 100000)
        result['isolate_policy'] = c.choice(data.get('isolate_policy'), ('all_isolates', 'first_per_patient'), 'isolate counting policy')
        return result
    result = {'patient_id': c.text(data.get('patient_id'), 'Patient', 100)}
    if kind == 'isolation':
        result.update({field: c.text(data.get(field), label, maximum) for field, label, maximum in (
            ('precautions', 'Authored precautions', 4000), ('start_reason', 'Authored start reason', 4000), ('authority_reference', 'Clinical authority / evidence reference', 500))})
        result['started_at'] = c.instant(data.get('started_at'), 'Recorded isolation start')
        result['case_id'] = c.text(data.get('case_id'), 'Linked surveillance case', 100, False)
        return result
    result['window_id'] = c.text(data.get('window_id'), 'Observation window', 100)
    result['evidence_reference'] = c.text(data.get('evidence_reference'), 'Source evidence reference', 500)
    if kind == 'case':
        result['episode_reference'] = c.text(data.get('episode_reference'), 'Distinct episode reference under the counting policy', 200)
        result['event_date'] = _day(data.get('event_date'), 'Event date')
        result['statement'] = c.text(data.get('statement'), 'Clinician-authored case evidence', 10000)
    elif kind == 'exposure':
        result['observed_date'] = _day(data.get('observed_date'), 'Exposure day')
        result['device_reference'] = c.text(data.get('device_reference'), 'Device reference', 200, False) or ''
        # There is no interval-to-day conversion, midnight rounding or inferred census.
        c.number(data.get('day_count', 1), 'Exactly observed day count', 1, 1)
        result['day_count'] = 1
    else:
        result.update({field: c.text(data.get(field), label, maximum) for field, label, maximum in (
            ('isolate_reference', 'Distinct isolate reference', 200), ('organism_code', 'Hospital organism code', 100),
            ('specimen_code', 'Hospital specimen code', 100), ('antibiotic_code', 'Hospital antibiotic code', 100),
            ('standard_reference', 'Laboratory interpretation standard and edition', 300), ('method', 'Recorded test method', 300),
            ('validator_reference', 'Validated laboratory result / reviewer reference', 500))})
        for field in ('organism_code', 'specimen_code', 'antibiotic_code'):
            result[field] = result[field].upper()
        result['specimen_date'] = _day(data.get('specimen_date'), 'Specimen date')
        result['result'] = c.choice(data.get('result'), ('S', 'I', 'R'), 'validated laboratory interpretation')
    return result


def create(kind, data, *, actor, key, request_meta=None):
    payload = _payload(kind, data)
    key = c.text(key, 'Idempotency-Key', 150)
    digest = c.fingerprint({'kind': kind, 'payload': payload})
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, MANAGE if kind in ('definition', 'window') else WRITE)
        old = db.execute('SELECT * FROM infection_requests WHERE facility_id=1 AND actor_id=? AND client_key=?', (actor['id'], key)).fetchone()
        if old:
            if old['request_hash'] != digest or old['record_kind'] != kind:
                raise c.OperationsError('This request key was used for different information.', 'idempotency_conflict', 409)
            row = _window(db, old['record_id'], actor) if kind == 'window' else _get(db, kind, old['record_id'], actor)
            c.audit_on(db, actor, 'infection_surveillance', row['id'], 'replayed', patient_id=row.get('patient_id'), meta=request_meta, read=True)
            row.pop('finalized_counts_json', None)
            return row
        if payload.get('patient_id'):
            c.patient_on(db, payload['patient_id'], actor)
        if kind == 'window':
            c.scoped_on(db, TABLES['definition'], payload['definition_id'], actor)
        elif kind in ('case', 'exposure', 'susceptibility'):
            window = _window(db, payload['window_id'], actor, writable=True)
            _within(window, payload.get('event_date') or payload.get('observed_date') or payload['specimen_date'])
            if kind == 'exposure':
                definition = _get(db, 'definition', window['definition_id'], actor)
                if definition['denominator_unit'] == 'device_days' and not payload['device_reference']:
                    raise c.OperationsError('A distinct device reference is required for an observed device-day.')
                if definition['denominator_unit'] == 'patient_days' and payload['device_reference']:
                    raise c.OperationsError('A patient-day denominator cannot contain a device reference.')
            elif kind == 'susceptibility':
                prior = db.execute("SELECT * FROM infection_susceptibility WHERE window_id=? AND isolate_reference=? AND status='active' LIMIT 1", (window['id'], payload['isolate_reference'])).fetchone()
                if prior and any(prior[field] != payload[field] for field in ('patient_id', 'specimen_date', 'organism_code', 'specimen_code', 'standard_reference', 'method')):
                    raise c.OperationsError('This isolate reference already identifies different patient, specimen or interpretation information.', 'isolate_conflict', 409)
        elif kind == 'isolation' and payload['case_id']:
            case = _get(db, 'case', payload['case_id'], actor)
            if case['patient_id'] != payload['patient_id']:
                raise c.OperationsError('Linked case does not belong to this patient.', 'not_found', 404)
        ident, stamp = c.new_id('ICS'), c.now()
        values = {'id': ident, 'department': actor['department'], **payload, 'created_by': actor['id'], 'created_at': stamp}
        if kind != 'definition':
            values['updated_at'] = stamp
        columns = ','.join(values)
        try:
            db.execute(f"INSERT INTO {TABLES[kind]} ({columns}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
        except DBIntegrityError:
            raise c.OperationsError('This definition code, episode, exposure day or isolate result is already recorded. Open the existing record before retrying.', 'duplicate_record', 409) from None
        row = _get(db, kind, ident, actor)
        _event(db, actor, kind, row, 'recorded', 'Manual source record entered.', request_meta)
        db.execute('INSERT INTO infection_requests(id,actor_id,client_key,request_hash,record_kind,record_id,patient_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                   (c.new_id('ICR'), actor['id'], key, digest, kind, ident, payload.get('patient_id'), stamp))
        row.pop('finalized_counts_json', None)
        return row


def register(*, actor, limit=100, offset=0, request_meta=None):
    c.number(limit, 'Limit', 1, 200); c.number(offset, 'Offset', 0, 100000)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, READ)
        where, args = c.scope_sql(actor)
        patient_where, patient_args = c.patient_filter(actor)
        result = {}
        for key, table in (('definitions', TABLES['definition']), ('windows', TABLES['window']), ('isolations', TABLES['isolation'])):
            extra, extra_args = (' AND ' + patient_where, patient_args) if key == 'isolations' else ('', ())
            result[key] = [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE {where}{extra} ORDER BY created_at DESC,id LIMIT ? OFFSET ?', (*args, *extra_args, limit + 1, offset))]
        for row in result['windows']:
            row.pop('finalized_counts_json', None)
        result['has_more'] = any(len(rows) > limit for rows in result.values())
        for key in ('definitions', 'windows', 'isolations'):
            result[key] = result[key][:limit]
        c.audit_on(db, actor, 'infection_surveillance', 'register', 'listed', meta=request_meta, read=True)
        return {**result, 'limit': limit, 'offset': offset, 'notice': NOTICE}


def detail(kind, ident, *, actor, limit=100, offset=0, request_meta=None):
    if kind not in TABLES:
        raise c.OperationsError('Unknown record type.')
    c.number(limit, 'Limit', 1, 200); c.number(offset, 'Offset', 0, 100000)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, READ)
        row = _window(db, ident, actor) if kind == 'window' else _get(db, kind, ident, actor)
        row['events'] = [dict(r) for r in db.execute('SELECT * FROM infection_events WHERE record_kind=? AND record_id=? ORDER BY occurred_at,id', (kind, ident))]
        if kind == 'window':
            row['definition'] = _get(db, 'definition', row['definition_id'], actor)
            row['has_more'] = False
            for name, table in (('cases', TABLES['case']), ('exposures', TABLES['exposure']), ('susceptibility', TABLES['susceptibility'])):
                records = [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE window_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?', (ident, limit + 1, offset))]
                row['has_more'] |= len(records) > limit
                row[name] = records[:limit]
            row.update({'limit': limit, 'offset': offset})
        c.audit_on(db, actor, 'infection_surveillance', ident, kind + '_viewed', patient_id=row.get('patient_id'), meta=request_meta, read=True)
        row.pop('finalized_counts_json', None)
        return row


def classify(ident, data, *, actor, request_meta=None):
    status = c.choice(data.get('status'), ('included', 'excluded', 'under_review'), 'case classification')
    reason = c.text(data.get('reason'), 'Authored classification reason', 4000)
    reviewer = c.text(data.get('reviewer_reference'), 'Clinician decision / reviewer reference', 500)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, MANAGE)
        row = _get(db, 'case', ident, actor); _window(db, row['window_id'], actor, writable=True)
        c.revision(row, data.get('version'))
        db.execute('UPDATE infection_cases SET status=?,classification_reason=?,reviewer_reference=?,updated_at=?,version=version+1 WHERE id=?', (status, reason, reviewer, c.now(), ident))
        row = _get(db, 'case', ident, actor)
        _event(db, actor, 'case', row, 'classified_' + status, reason + '\nDecision reference: ' + reviewer, request_meta)
        return row


def void(kind, ident, data, *, actor, request_meta=None):
    if kind not in ('exposure', 'susceptibility'):
        raise c.OperationsError('Only exposure and susceptibility source rows may be voided.')
    reason = c.text(data.get('reason'), 'Correction reason', 4000)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, WRITE)
        row = _get(db, kind, ident, actor); _window(db, row['window_id'], actor, writable=True)
        c.revision(row, data.get('version'))
        if row['status'] != 'active':
            raise c.OperationsError('This source row is already void.', 'invalid_transition', 409)
        db.execute(f"UPDATE {TABLES[kind]} SET status='void',void_reason=?,updated_at=?,version=version+1 WHERE id=?", (reason, c.now(), ident))
        row = _get(db, kind, ident, actor)
        _event(db, actor, kind, row, 'voided', reason, request_meta)
        return row


def end_isolation(ident, data, *, actor, request_meta=None):
    end = c.instant(data.get('ended_at'), 'Recorded isolation end')
    reason = c.text(data.get('reason'), 'Authored end reason', 4000)
    authority = c.text(data.get('authority_reference'), 'Clinical end authority / evidence reference', 500)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, WRITE)
        row = _get(db, 'isolation', ident, actor); c.revision(row, data.get('version'))
        if row['status'] != 'active' or end < row['started_at']:
            raise c.OperationsError('End time must follow the start of an active isolation record.', 'invalid_transition', 409)
        db.execute("UPDATE infection_isolation_records SET status='ended',ended_at=?,end_reason=?,end_authority_reference=?,updated_at=?,version=version+1 WHERE id=?", (end, reason, authority, c.now(), ident))
        row = _get(db, 'isolation', ident, actor)
        _event(db, actor, 'isolation', row, 'ended', reason + '\nDecision reference: ' + authority, request_meta)
        return row


def _counts(db, window_id):
    return {kind: db.execute(f"SELECT COUNT(*) FROM {TABLES[kind]} WHERE window_id=?" +
                            (" AND status='active'" if kind != 'case' else ''), (window_id,)).fetchone()[0]
            for kind in ('case', 'exposure', 'susceptibility')}


def window_action(ident, data, *, actor, request_meta=None):
    action = c.choice(data.get('action'), ('finalize', 'reopen'), 'window action')
    reason = c.text(data.get('reason'), 'Population / completeness attestation or correction reason', 4000)
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, MANAGE)
        row = _window(db, ident, actor); c.revision(row, data.get('version'))
        if row['status'] != ('open' if action == 'finalize' else 'finalized'):
            raise c.OperationsError('That action is not available in this window state.', 'invalid_transition', 409)
        if action == 'finalize':
            if row['end_date'] >= hospital_today().isoformat():
                raise c.OperationsError('The inclusive observation window must have ended before it is finalized.', 'window_not_ended', 409)
            if db.execute("SELECT 1 FROM infection_cases WHERE window_id=? AND status='under_review' LIMIT 1", (ident,)).fetchone():
                raise c.OperationsError('Review every pending case before finalizing.', 'pending_cases', 409)
            if not db.execute("SELECT 1 FROM infection_exposure_days WHERE window_id=? AND status='active' LIMIT 1", (ident,)).fetchone():
                raise c.OperationsError('Record the observed denominator days before finalizing.', 'missing_denominator', 409)
            db.execute("UPDATE infection_windows SET status='finalized',finalized_at=?,finalized_by=?,finalization_note=?,finalized_counts_json=?,updated_at=?,version=version+1 WHERE id=?", (c.now(), actor['id'], reason, json.dumps(_counts(db, ident), sort_keys=True), c.now(), ident))
        else:
            db.execute("UPDATE infection_windows SET status='open',finalized_at=NULL,finalized_by=NULL,finalization_note=NULL,finalized_counts_json=NULL,updated_at=?,version=version+1 WHERE id=?", (c.now(), ident))
        row = _window(db, ident, actor)
        _event(db, actor, 'window', row, action, reason, request_meta)
        row.pop('finalized_counts_json', None)
        return row


def metrics(ident, *, actor, group_by='organism,antibiotic', request_meta=None):
    grouping = group_by.split(',') if isinstance(group_by, str) else []
    if grouping not in (['organism', 'antibiotic'], ['organism', 'antibiotic', 'specimen']):
        raise c.OperationsError('Group by organism and antibiotic, optionally also specimen.')
    with db_session(immediate=True) as db:
        actor = c.actor_on(db, actor, READ)
        window = _window(db, ident, actor)
        if window['status'] != 'finalized':
            raise c.OperationsError('Finalize the explicit population, case review and denominator before calculating results.', 'window_not_finalized', 409)
        if window['finalized_counts_json'] != json.dumps(_counts(db, ident), sort_keys=True):
            raise c.OperationsError('Retained source records changed after finalization. Reopen, review the population and finalize again.', 'population_changed', 409)
        denominator = db.execute("SELECT COUNT(*) FROM infection_exposure_days WHERE window_id=? AND status='active'", (ident,)).fetchone()[0]
        numerator = db.execute("SELECT COUNT(*) FROM infection_cases WHERE window_id=? AND status='included'", (ident,)).fetchone()[0]
        minimum = window['minimum_cell']
        suppressed = denominator < minimum or (0 < numerator < minimum)
        rate = {'suppressed': suppressed, 'numerator': None if suppressed else numerator,
                'denominator': None if suppressed else denominator,
                'multiplier': window['rate_multiplier'],
                'value': None if suppressed or not denominator else round(numerator / denominator * window['rate_multiplier'], 6)}
        records = [dict(row) for row in db.execute("SELECT * FROM infection_susceptibility WHERE window_id=? AND status='active' ORDER BY specimen_date,isolate_reference,id LIMIT ?", (ident, MAX_REPORT_ROWS + 1))]
        if len(records) > MAX_REPORT_ROWS:
            raise c.OperationsError('This window exceeds the local report limit. Create narrower observation windows.', 'report_limit', 422)
        if window['isolate_policy'] == 'first_per_patient':
            first = {}
            for row in records:
                key = (row['patient_id'], row['organism_code'], row['specimen_code'] if 'specimen' in grouping else None, row['standard_reference'], row['method'])
                first.setdefault(key, row['isolate_reference'])
            records = [row for row in records if row['isolate_reference'] == first[(row['patient_id'], row['organism_code'], row['specimen_code'] if 'specimen' in grouping else None, row['standard_reference'], row['method'])]]
        cells = defaultdict(list)
        for row in records:
            key = (row['organism_code'], row['antibiotic_code'], row['specimen_code'] if 'specimen' in grouping else None,
                   row['standard_reference'], row['method'])
            cells[key].append(row['result'])
        antibiogram = []
        for (organism, antibiotic, specimen, standard, method), values in sorted(cells.items(), key=lambda item: json.dumps(item[0])):
            tested, hidden = len(values), len(values) < minimum
            counts = {result: values.count(result) for result in ('S', 'I', 'R')}
            # Also suppress rare outcome cells: a visible total and two other
            # outcomes would otherwise reconstruct the small cell by subtraction.
            hidden = hidden or any(0 < value < minimum for value in counts.values())
            antibiogram.append({'organism': organism, 'antibiotic': antibiotic, 'specimen': specimen,
                                'standard_reference': standard, 'method': method, 'suppressed': hidden,
                                'tested': None if hidden else tested,
                                'counts': None if hidden else counts,
                                'susceptible_percent': None if hidden else round(counts['S'] / tested * 100, 2)})
        c.audit_on(db, actor, 'infection_surveillance', ident, 'metrics_viewed', meta=request_meta, read=True)
        definition = _get(db, 'definition', window['definition_id'], actor)
        return {'window_id': ident, 'definition': definition, 'population': window['population_description'],
                'start_date': window['start_date'], 'end_date': window['end_date'], 'minimum_cell': minimum,
                'isolate_policy': window['isolate_policy'], 'group_by': grouping, 'rate': rate, 'antibiogram': antibiogram,
                'notice': 'Counts describe manually recorded included episodes and observed days, not inferred incidence. S, I and R retain the recorded laboratory interpretation; no therapy advice is generated.'}
