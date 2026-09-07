frappe.provide('prism');

frappe.pages['moodboard-style-workbench'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Moodboard Style Workbench',
		single_column: true,
	});
	wrapper.mswb = new prism.MoodboardStyleWorkbench(page, wrapper);
};

prism.MoodboardStyleWorkbench = class MoodboardStyleWorkbench {
	constructor(page, wrapper) {
		this.page = page;
		this.$body = $(wrapper).find('.layout-main-section');
		this.$body.addClass('mswb');

		// 4 Style3D file slots, mirrors the n/4 pill in the master list.
		this.SLOTS = [
			{ key: 'model_3d', label: '3D Model (GLB)', hint: 'GLB / GLTF', accept: '.glb,.gltf' },
			{ key: 'garment_file', label: 'Garment File', hint: 'Style3D source', accept: '.style3d,.zprj,.sproj,.zip' },
			{ key: 'bom', label: 'BOM', hint: 'XLSX / CSV / PDF', accept: '.xlsx,.csv,.pdf' },
			{ key: 'product_video', label: 'Product Video', hint: 'MP4 / MOV / WEBM', accept: '.mp4,.mov,.webm,.m4v' },
		];

		this.state = {
			search: '',
			page: 1,
			page_size: 20,
			selected: null,
			records: [],
			total: 0,
			detail_data: null,
		};

		this._read_hash();
		this._build_shell();
		this._wire_search();
		this._fetch_list().then(() => {
			if (this.state.selected) {
				this._load_detail(this.state.selected);
			} else if (this.state.records.length) {
				this._select_record(this.state.records[0].name);
			}
		});
	}

	// ---------- shell ----------
	_build_shell() {
		this.$body.html(`
			<div class="mswb-layout">
				<aside class="mswb-master" data-role="master">
					<div class="mswb-master-head">
						<div class="mswb-search-wrap">
							<input type="text" class="form-control input-sm" data-role="search" placeholder="Search PRA number, garment, category…">
						</div>
					</div>
					<div class="mswb-master-meta" data-role="listmeta">All styles</div>
					<div class="mswb-master-list" data-role="list">
						<div class="mswb-empty">Loading…</div>
					</div>
					<div class="mswb-master-pager" data-role="pager"></div>
				</aside>
				<section class="mswb-detail" data-role="detail">
					<div class="mswb-detail-empty">Select a Moodboard Style to view its details.</div>
				</section>
			</div>
		`);

		this.$els = {
			list: this.$body.find('[data-role="list"]'),
			listmeta: this.$body.find('[data-role="listmeta"]'),
			pager: this.$body.find('[data-role="pager"]'),
			detail: this.$body.find('[data-role="detail"]'),
			search: this.$body.find('[data-role="search"]'),
		};
		this.$els.search.val(this.state.search);
	}

	_wire_search() {
		let debounce_timer = null;
		this.$els.search.on('input', (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(debounce_timer);
			debounce_timer = setTimeout(() => {
				this.state.search = val;
				this.state.page = 1;
				this._fetch_list();
			}, 300);
		});
	}

	// ---------- hash sync ----------
	_read_hash() {
		const hash = (window.location.hash || '').replace(/^#/, '');
		if (!hash) return;
		const params = new URLSearchParams(hash);
		const selected = params.get('name');
		if (selected) this.state.selected = selected;
		const q = params.get('q');
		if (q) this.state.search = q;
	}

	_write_hash() {
		const params = new URLSearchParams();
		if (this.state.selected) params.set('name', this.state.selected);
		if (this.state.search) params.set('q', this.state.search);
		const h = params.toString();
		window.history.replaceState(null, '', h ? '#' + h : window.location.pathname);
	}

	// ---------- master list ----------
	_fetch_list(silent = false) {
		if (!silent) {
			this.$els.list.html('<div class="mswb-empty">Loading…</div>');
		}
		return frappe.call({
			method: 'prism.api.moodboard_style_workbench.get_list',
			args: {
				search: this.state.search,
				page: this.state.page,
				page_size: this.state.page_size,
			},
		}).then((r) => {
			const res = r.message || {};
			if (!res.status) {
				this.$els.list.html(`<div class="mswb-empty error">${frappe.utils.escape_html(res.error || 'Failed to load.')}</div>`);
				this.$els.pager.empty();
				return;
			}
			const d = res.data || {};
			this.state.records = d.rows || [];
			this.state.total = d.total || 0;
			this._render_list();
			this._render_pager();
		});
	}

	_render_list() {
		const rows = this.state.records;
		const esc = frappe.utils.escape_html;
		const term = (this.state.search || '').trim();
		this.$els.listmeta.text(
			term ? `${this.state.total} match${this.state.total === 1 ? '' : 'es'}` : 'All styles'
		);
		if (!rows.length) {
			this.$els.list.html('<div class="mswb-empty">No styles found.</div>');
			return;
		}
		const html = rows.map((row) => {
			const active = row.name === this.state.selected ? ' is-active' : '';
			const sub = [row.garment_name, row.product_category].filter(Boolean).join(' · ') || '—';
			const n = row.files_count || 0;
			const cls = n === 4 ? 'full' : n > 0 ? 'part' : '';
			return `
				<div class="mswb-list-item${active}" data-name="${esc(row.name)}">
					<div class="mswb-list-info">
						<div class="mswb-list-title">${esc(row.name)}</div>
						<div class="mswb-list-sub" title="${esc(sub)}">${esc(sub)}</div>
					</div>
					<span class="mswb-pill ${cls}"><span class="mswb-bead"></span>${n}/4</span>
				</div>
			`;
		}).join('');
		this.$els.list.html(html);
		this.$els.list.find('.mswb-list-item').on('click', (e) => {
			this._select_record($(e.currentTarget).attr('data-name'));
		});
	}

	_render_pager() {
		const total = this.state.total;
		const page = this.state.page;
		const size = this.state.page_size;
		const total_pages = Math.max(1, Math.ceil(total / size));
		const from = total === 0 ? 0 : (page - 1) * size + 1;
		const to = Math.min(total, page * size);

		this.$els.pager.html(`
			<div class="mswb-pager-info">${from}–${to} of ${total}</div>
			<div class="mswb-pager-btns">
				<button class="btn btn-default btn-xs" data-role="prev" ${page <= 1 ? 'disabled' : ''}>‹</button>
				<span class="mswb-pager-page">${page}/${total_pages}</span>
				<button class="btn btn-default btn-xs" data-role="next" ${page >= total_pages ? 'disabled' : ''}>›</button>
			</div>
		`);
		this.$els.pager.find('[data-role="prev"]').on('click', () => {
			if (this.state.page > 1) { this.state.page -= 1; this._fetch_list(); }
		});
		this.$els.pager.find('[data-role="next"]').on('click', () => {
			if (this.state.page < total_pages) { this.state.page += 1; this._fetch_list(); }
		});
	}

	_select_record(name) {
		if (!name) return;
		this.state.selected = name;
		this.$els.list.find('.mswb-list-item').removeClass('is-active');
		this.$els.list.find(`.mswb-list-item[data-name="${name}"]`).addClass('is-active');
		this._write_hash();
		this._load_detail(name);
	}

	// ---------- detail panel ----------
	_load_detail(name) {
		this.$els.detail.html('<div class="mswb-detail-empty">Loading…</div>');
		frappe.call({
			method: 'prism.api.moodboard_style_workbench.get_record',
			args: { name },
		}).then((r) => {
			const res = r.message || {};
			if (!res.status) {
				this.$els.detail.html(`<div class="mswb-detail-empty error">${frappe.utils.escape_html(res.error || 'Failed to load record.')}</div>`);
				return;
			}
			this.state.detail_data = res.data || {};
			this._render_detail(this.state.detail_data);
		}).catch((err) => {
			this.$els.detail.html(`<div class="mswb-detail-empty error">${frappe.utils.escape_html((err && err.message) || 'Request failed.')}</div>`);
		});
	}

	_filled(data) {
		const files = data.files || {};
		return this.SLOTS.filter((s) => files[s.key]).length;
	}

	_render_detail(data) {
		const esc = frappe.utils.escape_html;
		const n = this._filled(data);
		const cls = n === 4 ? 'full' : n > 0 ? 'part' : '';
		const created = this._fmt_date(data.creation);
		const image_btn = data.image
			? `<a class="btn btn-default btn-sm" href="${esc(data.image)}" target="_blank" rel="noopener" download>Download Style Image</a>`
			: '<span class="mswb-no-image">No Style image</span>';

		this.$els.detail.html(`
			<div class="mswb-detail-inner">
				<header class="mswb-detail-head">
					<div class="mswb-detail-titleblock">
						<h2 class="mswb-detail-title">${esc(data.name)}</h2>
						<div class="mswb-detail-meta">
							<span>${esc(data.garment_name || 'Untitled garment')}</span>
							<span class="mswb-sep">·</span>
							<span>${esc(created)}</span>
						</div>
					</div>
					<div class="mswb-detail-actions">
						${image_btn}
						<span class="mswb-pill ${cls}"><span class="mswb-bead"></span>${n}/4 files</span>
					</div>
				</header>

				<div class="mswb-sect">
					<h3 class="mswb-sect-title">Style Details</h3>
					<div class="mswb-meta-grid">
						${this._cell('PRA Number', esc(data.name))}
						${this._cell('Moodboard', `<span class="mswb-mono">${esc(data.moodboard_title || data.moodboard || '—')}</span>`)}
						${this._cell('Style No', esc(data.idx))}
						${this._cell('MOQ', esc(this._fmt_num(data.moq)))}
						${this._cell('Gender', esc(data.gender || '—'))}
						${this._cell('Category', esc(data.product_category || '—'))}
						${this._cell('Fabric Quality', esc(data.fabric_quality || '—'))}
						${this._cell('Style Type', esc(data.style || '—'))}
						${this._cell('Colour (TCX)', this._colour_swatch(data))}
					</div>
					<div class="mswb-desc-block">
						<label class="mswb-lbl">Description</label>
						<div class="mswb-desc">${esc(data.description || 'No description provided.')}</div>
					</div>
				</div>

				<div class="mswb-sect">
					<h3 class="mswb-sect-title">Fabric Elements · ${(data.fabrics || []).length}</h3>
					${this._render_fabric_table(data)}
				</div>

				<div class="mswb-sect mswb-sect-last">
					<h3 class="mswb-sect-title">Style3D Files</h3>
					${this.SLOTS.map((s) => this._render_upload_row(data, s)).join('')}
				</div>
			</div>
		`);

		this._wire_detail_actions(data);
	}

	_cell(label, value) {
		return `
			<div class="mswb-cell">
				<label class="mswb-lbl">${frappe.utils.escape_html(label)}</label>
				<div class="mswb-val">${value}</div>
			</div>`;
	}

	_render_fabric_table(data) {
		const esc = frappe.utils.escape_html;
		const fabrics = data.fabrics || [];
		if (!fabrics.length) {
			return '<div class="mswb-empty">No fabric elements found in the cost inputs for this style.</div>';
		}
		// Width is not present per-fabric in cost_inputs — show a hardcoded sample.
		const SAMPLE_WIDTH = '180 cm';

		const rows = fabrics.map((f) => {
			const blend = f.blend || '—';
			const construction = f.construction || '—';
			const gsm = (f.gsm || f.gsm === 0) ? esc(f.gsm) : '<span class="mswb-muted">—</span>';
			const consumption = (f.consumption || f.consumption === 0)
				? `${esc(f.consumption)} kg`
				: '<span class="mswb-muted">—</span>';
			const finish = f.finish || (f.print_type ? f.print_type : '—');
			return `
				<tr>
					<td><span class="mswb-frole">${esc(f.section || '—')}</span>
						<div class="mswb-fname" title="${esc(f.name || '')}">${esc(f.name || '')}</div></td>
					<td><span class="mswb-fstruct">${esc(construction)}</span></td>
					<td>${esc(blend)}</td>
					<td class="num">${gsm}</td>
					<td class="num">${consumption}</td>
					<td><span class="mswb-est">sample</span> ${esc(SAMPLE_WIDTH)}</td>
					<td>${esc(finish)}</td>
				</tr>`;
		}).join('');

		return `
			<table class="mswb-ftable">
				<thead>
					<tr>
						<th>Element</th><th>Construction</th><th>Blend</th>
						<th class="num">GSM</th><th class="num">Consumption</th>
						<th>Width</th><th>Finish / Print</th>
					</tr>
				</thead>
				<tbody>${rows}</tbody>
			</table>
			<div class="mswb-estnote">⚠ Width shown as a sample — not present in cost inputs; source from PSL Fabric Master for live values.</div>
		`;
	}

	_colour_swatch(data) {
		const esc = frappe.utils.escape_html;
		// Colour is held at the style (doc) level, not per fabric. Fall back to a
		// hardcoded sample shade when the doc has no element colour set.
		const hex = data.element_colour_hex || '#A4C8D4';
		const tcx = data.element_colour_tcx || '13-5411';
		const colour = data.element_colour || 'Sample Shade';
		return `
			<span class="mswb-swatch">
				<span class="mswb-chip" style="background:${esc(hex)}"></span>
				<span><span class="mswb-ccode">${esc(tcx)}</span><br><span class="mswb-cname">${esc(colour)}</span></span>
			</span>`;
	}

	_render_upload_row(data, slot) {
		const esc = frappe.utils.escape_html;
		const val = (data.files || {})[slot.key];
		const has = !!val;
		const icon = has ? '✓' : '↑';
		const state = has
			? `<a class="mswb-lnk" href="${esc(val)}" target="_blank" rel="noopener">${esc(this._file_label(val))}</a>`
			: 'No file uploaded';
		return `
			<div class="mswb-ufile ${has ? 'has' : ''}">
				<div class="mswb-uicon">${icon}</div>
				<div class="mswb-ubody">
					<div class="mswb-ulabel">${esc(slot.label)}</div>
					<div class="mswb-ustate">${state}</div>
				</div>
				<span class="mswb-uext">${esc(slot.hint)}</span>
				<div class="mswb-uactions">
					<button class="btn btn-${has ? 'default' : 'primary'} btn-xs" data-role="upload" data-key="${esc(slot.key)}">${has ? 'Replace' : 'Upload'}</button>
				</div>
			</div>`;
	}

	_wire_detail_actions(data) {
		this.$els.detail.find('[data-role="upload"]').on('click', (e) => {
			const key = $(e.currentTarget).attr('data-key');
			this._upload_file(key);
		});
	}

	_upload_file(key) {
		const name = this.state.selected;
		if (!name) return;
		const slot = this.SLOTS.find((s) => s.key === key);
		new frappe.ui.FileUploader({
			doctype: 'Moodboard Style',
			docname: name,
			dialog_title: __('Upload {0}', [slot ? slot.label : key]),
			allow_multiple: false,
			restrictions: slot && slot.accept ? { allowed_file_types: slot.accept.split(',') } : {},
			on_success: (file_doc) => {
				const file_url = file_doc && file_doc.file_url;
				if (!file_url) return;
				// Persist onto the doc field; before_save offloads it to S3.
				frappe.call({
					method: 'frappe.client.set_value',
					args: { doctype: 'Moodboard Style', name, fieldname: key, value: file_url },
				}).then(() => {
					frappe.show_alert({ message: __('{0} uploaded.', [slot ? slot.label : key]), indicator: 'green' }, 4);
					this._load_detail(name);
					this._fetch_list(true);
				});
			},
		});
	}

	// ---------- helpers ----------
	_file_label(url) {
		if (!url) return '';
		try {
			const clean = String(url).split('?')[0];
			return decodeURIComponent(clean.substring(clean.lastIndexOf('/') + 1)) || clean;
		} catch (e) {
			return url;
		}
	}

	_fmt_num(val) {
		const n = Number(val);
		if (!isFinite(n)) return val == null ? '—' : String(val);
		return n.toLocaleString('en-IN');
	}

	_fmt_date(value) {
		if (!value) return '';
		try {
			return frappe.datetime.str_to_user(value);
		} catch (e) {
			return value;
		}
	}
};
