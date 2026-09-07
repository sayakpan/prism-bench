'''
One-time migration: split the monolithic Moodboard.draft_layout_json blob into the
normalized fields / child tables / standalone doctypes.

Non-destructive: the old draft_layout_json is left untouched (rollback source).
Idempotent per board: standalone children (Version/Message/Style) are deleted and
recreated; parent columns + child tables + JSON fields are overwritten; Brand
Moodboard links are upserted. Unresolved master references are logged, not fatal.

Run one board:   prism.lib.moodboard_migrate.migrate_one("l01se2huoo")
Run all:         prism.lib.moodboard_migrate.run()
'''

import frappe

import prism.api.moodboard_style as ms

_STATUS = {'draft': 'Draft', 'published': 'Published',
           'unpublished changes': 'Unpublished Changes', 'unpublished': 'Published'}


def migrate_one(name, commit=True):
    report = {'moodboard': name, 'versions': 0, 'messages': 0, 'styles': 0,
              'fabrics': 0, 'garments': 0, 'brands': 0, 'unresolved': []}

    doc = frappe.get_doc('Moodboard', name)
    d = frappe.parse_json(doc.draft_layout_json or '{}')
    if not isinstance(d, dict):
        report['unresolved'].append('draft_layout_json is not an object')
        return report

    meta = d.get('meta') or {}
    ev2 = d.get('editorV2') or {}
    f = ev2.get('foundation') or {}

    # ---- core scalars / links ----
    doc.status = _STATUS.get(str(d.get('status') or 'draft').lower(), 'Draft')

    # Published boards keep a frozen snapshot for brand-facing reads. We preserve
    # the existing published_layout_json (the actual last-published content) as
    # the snapshot; it gets rebuilt in the normalized shape on the next republish.
    if doc.status == 'Published':
        pub = frappe.parse_json(doc.published_layout_json) if doc.published_layout_json else None
        doc.published_snapshot = _json(pub or d)
    else:
        doc.published_snapshot = None
    doc.season = _resolve_season(meta.get('season'), report)
    doc.creativity_bias = f.get('creativity_bias') or None
    doc.mood = meta.get('mood') or None
    doc.user_vision = meta.get('userVision') or None
    doc.brief = f.get('brief') or meta.get('aiPrompt') or None
    doc.image_model = meta.get('imageModel') or None
    doc.image_size = meta.get('imageSize') or None
    doc.thumbnail = ev2.get('IMAGE_thumbnail') or None

    st = ev2.get('alignment_settings') or {}
    doc.fabric_auto_suggest = 1 if st.get('fabric_auto_suggest') else 0
    doc.fabric_auto_count = _int(st.get('fabric_auto_count'))
    doc.style_auto_suggest = 1 if st.get('style_auto_suggest') else 0
    doc.style_auto_count = _int(st.get('style_auto_count'))
    doc.variant_count = _int(st.get('variant_count'))

    # ---- JSON fields ----
    doc.genders = _json(meta.get('gender'))
    doc.style_categories = _json(meta.get('styleCategory'))
    doc.mood_tags = _json(meta.get('moodTags'))
    doc.colours = _json(f.get('colours'))
    doc.alignment_report = _json(f.get('alignment_report'))
    doc.addons = _json(ev2.get('addons'))

    # ---- fabrics (child table) ----
    doc.set('fabrics', [])
    for x in (f.get('fabrics') or []):
        link = (frappe.db.get_value('Moodboard Dyed Fabric', {'code': x.get('code')}, 'name')
                or (frappe.db.get_value('Moodboard Dyed Fabric', {'batch': x.get('batch')}, 'name')
                    if x.get('batch') else None))
        if (x.get('code') or x.get('batch')) and not link:
            report['unresolved'].append(f"fabric code={x.get('code')} batch={x.get('batch')}")
        # Old blobs persisted only the chosen fabrics → mark them selected.
        # Store the full attribute payload (relevancy_score wasn't persisted historically).
        doc.append('fabrics', {
            'fabric': link, 'selected': 1, 'source_id': x.get('id'),
            'fabric_name': x.get('name'), 'code': x.get('code'), 'description': x.get('description'),
            'custom_fabric_name': x.get('customFabricName'), 'quality': x.get('quality'),
            'blend': x.get('blend'), 'composition': x.get('composition'), 'gsm': _int(x.get('gsm')),
            'shade_category': x.get('shadeCategory'), 'finish': x.get('finish'), 'batch': x.get('batch'),
            'image_url': x.get('imageUrl'), 'relevancy_score': x.get('relevancyScore'),
        })
        report['fabrics'] += 1

    # ---- garments (child table) ----
    doc.set('garments', [])
    for x in (f.get('styles') or []):
        gsr = x.get('gsrNo') or x.get('gsr_no')
        link = frappe.db.get_value('Sample Request', {'gsr_no': gsr}, 'name') if gsr else None
        if gsr and not link:
            report['unresolved'].append(f"garment gsr_no={gsr}")
        # Old blobs persisted only the chosen garments → mark them selected.
        # Store the full attribute payload (blob keys are camelCase; category ->
        # product_category, sampleColour -> element_colour, name -> garment_name).
        doc.append('garments', {
            'garment': link, 'selected': 1, 'source_id': x.get('id'),
            'gsr_no': gsr, 'garment_name': x.get('name'),
            'gender': x.get('gender'), 'product_category': x.get('category'),
            'fabric_quality': x.get('fabricQuality'), 'fabric_blend': x.get('fabricBlend'),
            'element_colour': x.get('sampleColour'), 'finished_gsm': x.get('finishedGsm'),
            'image_urls': _json(x.get('imageUrls')),
            'cleaned_front_image': x.get('IMAGE_cleanedFrontImage') or x.get('cleanedFrontImage'),
            'cleaned_back_image': x.get('IMAGE_cleanedBackImage') or x.get('cleanedBackImage'),
        })
        report['garments'] += 1

    doc.save(ignore_permissions=True)

    # ---- versions (standalone) ----
    # Raw SQL delete (not frappe.delete_doc) so re-runs don't enqueue a
    # delete_dynamic_links background job per row (those pile past the queue cap).
    frappe.db.delete('Moodboard Version', {'moodboard': name})
    idmap = {}
    versions = ev2.get('versions') or []
    for v in versions:
        vd = frappe.new_doc('Moodboard Version')
        vd.moodboard = name
        vd.version_key = v.get('id')
        vd.prompt = v.get('prompt')
        vd.llm_prompt = v.get('llmPrompt')
        vd.ai_reply = v.get('aiReply')
        vd.image_model = v.get('imageModel')
        vd.image_size = v.get('imageSize')
        vd.generation_time = v.get('generationTime')
        vd.is_showable = 1 if v.get('isShowable') else 0
        vd.response_id = v.get('responseId')
        vd.response_created_at = _dt(v.get('responseCreatedAt'))
        vd.edited_at = _dt(v.get('editedAt'))
        vd.image = v.get('IMAGE_image')
        vd.edited_image = v.get('editedImage')
        vd.canvas_state = _json(v.get('canvasState'))
        vd.insert(ignore_permissions=True)
        if v.get('createdAt'):
            frappe.db.set_value('Moodboard Version', vd.name, 'creation', _dt(v.get('createdAt')),
                                update_modified=False)
        idmap[v.get('id')] = vd.name
        report['versions'] += 1
    for v in versions:
        pv = v.get('parentVersionId')
        if pv and idmap.get(v.get('id')) and idmap.get(pv):
            frappe.db.set_value('Moodboard Version', idmap[v['id']], 'parent_version', idmap[pv])
    pid = ev2.get('primary_version_id') or ev2.get('primaryVersionId')
    frappe.db.set_value('Moodboard', name, 'primary_version', idmap.get(pid))

    # ---- messages (standalone) ----
    frappe.db.delete('Moodboard Message', {'moodboard': name})
    for m in (ev2.get('messages') or []):
        md = frappe.new_doc('Moodboard Message')
        md.moodboard = name
        md.message_key = m.get('id')
        md.role = m.get('role')
        md.text = m.get('text')
        md.version = idmap.get(m.get('versionId'))
        md.attachments = _json(m.get('attachments'))
        md.insert(ignore_permissions=True)
        if m.get('createdAt'):
            frappe.db.set_value('Moodboard Message', md.name, 'creation', _dt(m.get('createdAt')),
                                update_modified=False)
        report['messages'] += 1

    # ---- styles + cost (reuse Moodboard Style API) ----
    extracted = f.get('extracted_styles') or []
    if extracted:
        # Pre-clear via raw delete so sync_styles has nothing to delete (its
        # reconcile uses frappe.delete_doc, which would enqueue jobs on re-runs).
        frappe.db.delete('Moodboard Style', {'moodboard': name})
        saved = ms.sync_styles(moodboard=name, styles=extracted)
        report['styles'] = len(saved)
        included = [s for s in extracted if s.get('include', True) not in (False, 0, '0')]
        old_to_new = {included[i].get('id'): saved[i]['id'] for i in range(min(len(included), len(saved)))}
        cc = ev2.get('cost_calculation') or {}
        for old_id, entry in cc.items():
            if old_id == '_meta' or not isinstance(entry, dict):
                continue
            totals = entry.get('totals')
            new_id = old_to_new.get(old_id)
            if not (new_id and totals):
                continue
            rollup = (totals.get('rollup') or {})
            ms.update_style_cost(
                id=new_id, inputs=entry.get('inputs'), result=entry.get('result'), totals=totals,
                currency=rollup.get('currencyCode') or (entry.get('_meta') or {}).get('currency'),
                fx_rate=(entry.get('_meta') or {}).get('fx'),
                order_quantity=((entry.get('inputs') or {}).get('print_sections') or {}).get('order_quantity'),
            )

    # ---- brands (upsert Brand Moodboard) ----
    for b in (meta.get('brands') or []):
        bid = b.get('id') if isinstance(b, dict) else b
        if not bid:
            continue
        if not frappe.db.exists('Brand', bid):
            report['unresolved'].append(f"brand id={bid}")
            continue
        if not frappe.db.exists('Brand Moodboard', {'brand': bid, 'moodboard': name}):
            bm = frappe.new_doc('Brand Moodboard')
            bm.brand = bid
            bm.moodboard = name
            bm.insert(ignore_permissions=True)
        report['brands'] += 1

    # ---- owner (drives the editor/viewer access model) ----
    owner_obj = d.get('owner') if isinstance(d.get('owner'), dict) else {}
    owner_email = owner_obj.get('email') or owner_obj.get('id')
    if owner_email and frappe.db.exists('User', owner_email):
        frappe.db.set_value('Moodboard', name, 'owner', owner_email, update_modified=False)

    if commit:
        frappe.db.commit()
    return report


def run_published_snapshots(limit=None):
    '''
    v1_1: for Published boards, rebuild `published_snapshot` in the NEW model shape
    from the **published_layout_json** blob (faithful to what was actually published,
    not the draft-derived normalized data — so unpublished edits never leak), and set
    `last_published_at` from the draft blob's createdAt.
    '''
    names = frappe.get_all('Moodboard', filters={'is_active': 1, 'status': 'Published'},
                           pluck='name', limit=limit)
    summary = {'total': len(names), 'ok': 0, 'failed': 0, 'errors': []}
    for n in names:
        try:
            published = frappe.parse_json(frappe.db.get_value('Moodboard', n, 'published_layout_json') or 'null')
            draft = frappe.parse_json(frappe.db.get_value('Moodboard', n, 'draft_layout_json') or '{}')
            src = published if isinstance(published, dict) else (draft if isinstance(draft, dict) else {})
            updates = {'published_snapshot': frappe.as_json(build_published_snapshot(n, src))}
            lpa = _dt((draft or {}).get('createdAt')) if isinstance(draft, dict) else None
            if lpa:
                updates['last_published_at'] = lpa
            frappe.db.set_value('Moodboard', n, updates, update_modified=False)
            summary['ok'] += 1
        except Exception as ex:
            summary['failed'] += 1
            summary['errors'].append({'moodboard': n, 'error': str(ex)})
            frappe.log_error(frappe.get_traceback(), f'published_snapshot {n}')
    frappe.db.commit()
    return summary


def build_published_snapshot(name, blob):
    '''
    Transform a published editorV2 blob into the new-model snapshot shape (the same
    structure moodboard_v2.get_moodboard returns for a brand: core + fabrics +
    garments + styles + primaryVersionImage). Brand-facing reads return this verbatim.
    '''
    blob = blob or {}
    meta = blob.get('meta') or {}
    ev2 = blob.get('editorV2') or {}
    f = ev2.get('foundation') or {}
    st = ev2.get('alignment_settings') or {}
    cost = ev2.get('cost_calculation') or {}

    # primary version image
    primary_id = ev2.get('primary_version_id') or ev2.get('primaryVersionId')
    primary_image = None
    for v in (ev2.get('versions') or []):
        if v.get('id') == primary_id:
            primary_image = v.get('IMAGE_image') or v.get('image')
            break

    return {
        'id': name,
        'title': blob.get('title'),
        'status': 'Published',
        'season': meta.get('season') or None,
        'creativityBias': f.get('creativity_bias') or None,
        'mood': meta.get('mood') or None,
        'userVision': meta.get('userVision') or None,
        'brief': f.get('brief') or meta.get('aiPrompt') or None,
        'imageModel': meta.get('imageModel') or None,
        'imageSize': meta.get('imageSize') or None,
        'thumbnail': ev2.get('IMAGE_thumbnail') or None,
        'primaryVersionImage': primary_image,
        'genders': meta.get('gender') or [],
        'styleCategories': meta.get('styleCategory') or [],
        'moodTags': meta.get('moodTags') or [],
        'colours': f.get('colours') or [],
        'alignmentReport': f.get('alignment_report') or None,
        'addons': ev2.get('addons') or None,
        'settings': {
            'fabricAutoSuggest': bool(st.get('fabric_auto_suggest')),
            'fabricAutoCount': st.get('fabric_auto_count'),
            'styleAutoSuggest': bool(st.get('style_auto_suggest')),
            'styleAutoCount': st.get('style_auto_count'),
            'variantCount': st.get('variant_count'),
        },
        'brands': [{'id': b.get('id'), 'name': b.get('name')}
                   for b in (meta.get('brands') or []) if isinstance(b, dict)],
        'fabrics': [_snap_fabric(x) for x in (f.get('fabrics') or [])],
        'garments': [_snap_garment(x) for x in (f.get('styles') or [])],
        'styles': [_snap_style(x, cost) for x in (f.get('extracted_styles') or [])],
    }


def _snap_fabric(x):
    return {
        'id': (frappe.db.get_value('Moodboard Dyed Fabric', {'code': x.get('code')}, 'name')
               or (frappe.db.get_value('Moodboard Dyed Fabric', {'batch': x.get('batch')}, 'name')
                   if x.get('batch') else None)),
        'selected': True, 'source_id': x.get('id'),
        'name': x.get('name'), 'code': x.get('code'), 'description': x.get('description'),
        'custom_fabric_name': x.get('customFabricName'), 'quality': x.get('quality'),
        'blend': x.get('blend'), 'composition': x.get('composition'), 'gsm': x.get('gsm'),
        'shade_category': x.get('shadeCategory'), 'finish': x.get('finish'), 'batch': x.get('batch'),
        'image_url': x.get('imageUrl'), 'relevancy_score': x.get('relevancyScore'),
    }


def _snap_garment(x):
    gsr = x.get('gsrNo') or x.get('gsr_no')
    return {
        'id': frappe.db.get_value('Sample Request', {'gsr_no': gsr}, 'name') if gsr else None,
        'selected': True, 'source_id': x.get('id'),
        'gsr_no': gsr, 'garment_name': x.get('name'), 'gender': x.get('gender'),
        'product_category': x.get('category'), 'fabric_quality': x.get('fabricQuality'),
        'fabric_blend': x.get('fabricBlend'), 'element_colour': x.get('sampleColour'),
        'finished_gsm': x.get('finishedGsm'), 'image_urls': x.get('imageUrls') or {},
        'relevancy_score': x.get('relevancyScore'),
        'cleaned_front_image': x.get('IMAGE_cleanedFrontImage') or x.get('cleanedFrontImage'),
        'cleaned_back_image': x.get('IMAGE_cleanedBackImage') or x.get('cleanedBackImage'),
    }


def _snap_style(x, cost):
    '''
    Brand-safe extracted style for the published snapshot: basic fields + attrs,
    with cost reduced to currency + fxRate only (no internal price breakdown).
    '''
    meta = (cost.get('_meta') or {}) if isinstance(cost, dict) else {}
    entry = cost.get(x.get('id')) if isinstance(cost, dict) else None
    totals = (entry or {}).get('totals') or {}
    cost_obj = None
    if totals:
        cost_obj = {
            'currency': (totals.get('rollup') or {}).get('currencyCode') or meta.get('currency'),
            'fxRate': meta.get('fx'),
        }
    return {
        'id': x.get('id'), 'image': x.get('IMAGE_image') or x.get('image'),
        'include': x.get('include', True), 'moq': x.get('moq'),
        'attrs': x.get('attrs') or {}, 'cost': cost_obj,
    }


def run(limit=None):
    # Note: this loop uses raw frappe.db.delete for child rows, so it does NOT
    # enqueue background jobs — safe to run inside a bench-migrate patch.
    names = frappe.get_all('Moodboard', filters={'is_active': 1}, pluck='name',
                           order_by='creation asc', limit=limit)
    summary = {'total': len(names), 'ok': 0, 'failed': 0, 'unresolved': 0, 'errors': []}
    for n in names:
        try:
            r = migrate_one(n, commit=True)
            summary['ok'] += 1
            summary['unresolved'] += len(r['unresolved'])
        except Exception as ex:
            frappe.db.rollback()
            summary['failed'] += 1
            summary['errors'].append({'moodboard': n, 'error': str(ex)})
            frappe.log_error(frappe.get_traceback(), f'moodboard_migrate {n}')
    return summary


def _probe(name):
    ''' Dev helper: migrate one board, return a one-line outcome string. '''
    import traceback
    try:
        r = migrate_one(name)
        m = frappe.get_doc('Moodboard', name)
        return ('OK versions=%s messages=%s styles=%s fabrics=%s garments=%s brands=%s '
                'status=%s snapshot=%s unresolved=%s' % (
                    r['versions'], r['messages'], r['styles'], r['fabrics'], r['garments'],
                    r['brands'], m.status, bool(m.published_snapshot), r['unresolved']))
    except Exception:
        return 'ERR ' + traceback.format_exc()[-1400:]


def audit_unresolved(limit=None):
    ''' Read-only: list every reference that won't resolve to a master. '''
    out = []
    for n in frappe.get_all('Moodboard', filters={'is_active': 1}, pluck='name', limit=limit):
        d = frappe.parse_json(frappe.db.get_value('Moodboard', n, 'draft_layout_json') or '{}')
        if not isinstance(d, dict):
            continue
        meta = d.get('meta') or {}
        f = (d.get('editorV2') or {}).get('foundation') or {}
        season = meta.get('season')
        if season and not (frappe.db.exists('Season', season)
                           or frappe.db.get_value('Season', {'code': season}, 'name')):
            out.append((n, 'season', season))
        for x in (f.get('fabrics') or []):
            link = (frappe.db.get_value('Moodboard Dyed Fabric', {'code': x.get('code')}, 'name')
                    or (frappe.db.get_value('Moodboard Dyed Fabric', {'batch': x.get('batch')}, 'name')
                        if x.get('batch') else None))
            if not link:
                out.append((n, 'fabric', x.get('code') or x.get('batch')))
        for x in (f.get('styles') or []):
            gsr = x.get('gsrNo') or x.get('gsr_no')
            if gsr and not frappe.db.get_value('Sample Request', {'gsr_no': gsr}, 'name'):
                out.append((n, 'garment', gsr))
        for b in (meta.get('brands') or []):
            bid = b.get('id') if isinstance(b, dict) else b
            if bid and not frappe.db.exists('Brand', bid):
                out.append((n, 'brand', bid))
    return out


# ---- helpers ----

def _resolve_season(code, report):
    if not code:
        return None
    if frappe.db.exists('Season', code):
        return code
    by_code = frappe.db.get_value('Season', {'code': code}, 'name')
    if by_code:
        return by_code
    report['unresolved'].append(f"season={code}")
    return None


def _json(value):
    if value in (None, ''):
        return None
    return frappe.as_json(value)


def _int(value):
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _dt(value):
    if not value:
        return None
    if not isinstance(value, str):
        return value
    s = value.strip().replace('T', ' ')
    if s.endswith('Z'):
        s = s[:-1]
    if ' ' in s:
        date_part, time_part = s.split(' ', 1)
        for sign in ('+', '-'):
            if sign in time_part:
                time_part = time_part.split(sign, 1)[0]
        s = f'{date_part} {time_part}'.strip()
    return s
