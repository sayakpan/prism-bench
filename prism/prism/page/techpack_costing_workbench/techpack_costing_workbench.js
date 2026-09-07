frappe.provide('prism');

frappe.pages['techpack-costing-workbench'].on_page_load = function(wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Techpack Costing',
		single_column: true,
	});
	wrapper.tcwb = new prism.TechpackCostingWorkbench(page, wrapper);
};

prism.TechpackCostingWorkbench = class TechpackCostingWorkbench {
	constructor(page, wrapper) {
		this.page = page;
		this.$body = $(wrapper).find('.layout-main-section');
		this.$body.addClass('tcwb');

		this.state = {
			search: '',
			page: 1,
			page_size: 20,
			selected: null,
			records: [],
			total: 0,
			detail_loading: false,
			expanded: {},
			detail_data: null,
			original_adjustments: [],
			adjustments_dirty: false,
			saving_adjustments: false,
		};
		this._costing_poll = {
			timer: null,
			name: null,
			attempts: 0,
			in_flight: false,
			token: 0,
			interval_ms: 5000,
			max_attempts: 96, // ~8 minutes
		};

		this._read_hash();
		this._build_shell();
		this._wire_search();
		this._setup_page_actions();
		$(window).off('hashchange.tcwb-alertpos').on('hashchange.tcwb-alertpos', () => this._sync_alert_container_position());
		$(window).off('beforeunload.tcwb').on('beforeunload.tcwb', () => {
			this._stop_costing_poll();
			$('#alert-container').removeClass('tcwb-alert-top-right').css({ top: '', bottom: '', right: '' });
		});
		this._sync_alert_container_position();
		this._fetch_list().then(() => {
			if (this.state.selected) {
				this._load_detail(this.state.selected);
			} else if (this.state.records.length) {
				this._select_record(this.state.records[0].name);
			}
		});
	}

	_setup_page_actions() {
		this.page.set_primary_action('+ Add Techpack', () => this._show_add_dialog(), '');
	}

	_download_current_pdf() {
		const name = this.state.selected;
		if (!name) {
			frappe.msgprint(__('Please select a Techpack Costing record first.'));
			return;
		}

		const params = new URLSearchParams({
			name,
		});
		const url = `/api/method/prism.api.techpack_costing.download_workbench_pdf?${params.toString()}`;
		window.open(url, '_blank');
	}

	_show_add_dialog() {
		const dlg = new frappe.ui.Dialog({
			title: 'Add Techpack',
			fields: [
				{
					fieldname: 'title',
					label: 'Title',
					fieldtype: 'Data',
					description: 'Optional. If left blank, the file name will be used.',
				},
				{
					fieldname: 'tech_pack',
					label: 'Techpack PDF',
					fieldtype: 'Attach',
					reqd: 1,
					options: { restrictions: { allowed_file_types: ['.pdf'] } },
				},
				{
					fieldname: 'artwork',
					label: 'Artwork PDF',
					fieldtype: 'Attach',
					description: 'Optional.',
					options: { restrictions: { allowed_file_types: ['.pdf'] } },
				},
				{
					fieldname: 'order_quantity',
					label: 'Order Quantity',
					fieldtype: 'Int',
					default: 1500,
					description: 'Used for print costing.',
				},
			],
			primary_action_label: 'Add',
			primary_action: (values) => {
				if (!values.tech_pack) {
					frappe.msgprint(__('Please attach a Techpack PDF.'));
					return;
				}

				const $btn = dlg.get_primary_btn();
				const restore_btn = () => $btn.prop('disabled', false).text('Add');
				$btn.prop('disabled', true).text('Adding…');

				const payload = { doctype: 'Techpack Costing', tech_pack: values.tech_pack };
				const t = (values.title || '').trim();
				if (t) payload.title = t;
				if (values.artwork) payload.artwork = values.artwork;
				if (values.order_quantity) payload.order_quantity = values.order_quantity;

				frappe.call({
					method: 'frappe.client.insert',
					args: { doc: payload },
				}).then((r) => {
					const doc = r && r.message;
					if (!doc || !doc.name) {
						restore_btn();
						return;
					}
					dlg.hide();
					frappe.show_alert({
						message: __('Techpack added. Cost calculation queued in the background.'),
						indicator: 'green',
					}, 5);
					this._sync_alert_container_position();
					this.state.page = 1;
					this.state.search = '';
					this.$els.search.val('');
					this._fetch_list().then(() => {
						this._select_record(doc.name);
						this._start_costing_poll(doc.name);
					});
				}, (err) => {
					restore_btn();
					console.error('Techpack insert failed', err);
					const msg = (err && (err.message || err.responseText)) || __('Failed to add techpack.');
					frappe.msgprint({ title: __('Error'), message: msg, indicator: 'red' });
				});
			},
		});
		dlg.show();
	}

	// ---------- shell ----------
	_build_shell() {
		this.$body.html(`
			<div class="tcwb-layout">
				<aside class="tcwb-master" data-role="master">
					<div class="tcwb-master-head">
						<div class="tcwb-search-wrap">
							<input type="text" class="form-control input-sm" data-role="search" placeholder="Search title, owner…">
						</div>
					</div>
					<div class="tcwb-master-list" data-role="list">
						<div class="tcwb-empty">Loading…</div>
					</div>
					<div class="tcwb-master-pager" data-role="pager"></div>
				</aside>
				<section class="tcwb-detail" data-role="detail">
					<div class="tcwb-detail-empty">Select a Techpack Costing record to see the breakdown.</div>
				</section>
			</div>
		`);

		this.$els = {
			master: this.$body.find('[data-role="master"]'),
			list: this.$body.find('[data-role="list"]'),
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
			this.$els.list.html('<div class="tcwb-empty">Loading…</div>');
		}
		return frappe.call({
			method: 'prism.api.techpack_costing.get_list',
			args: {
				search: this.state.search,
				page: this.state.page,
				page_size: this.state.page_size,
			},
		}).then((r) => {
			const res = r.message || {};
			if (!res.status) {
				this.$els.list.html(`<div class="tcwb-empty error">${frappe.utils.escape_html(res.error || 'Failed to load.')}</div>`);
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

	_start_costing_poll(name) {
		if (!name) return;
		this._stop_costing_poll();

		this._costing_poll.name = name;
		this._costing_poll.attempts = 0;
		this._costing_poll.in_flight = false;
		this._costing_poll.token += 1;
		const token = this._costing_poll.token;
		this._render_list();

		const tick = () => {
			const poll = this._costing_poll;
			if (token !== poll.token) return;
			if (!poll.name || poll.in_flight) return;
			if (poll.attempts >= poll.max_attempts) {
				this._stop_costing_poll();
				return;
			}

			poll.attempts += 1;
			poll.in_flight = true;
			const doc_name = poll.name;

			frappe.call({
				method: 'frappe.client.get_value',
				args: {
					doctype: 'Techpack Costing',
					filters: { name: doc_name },
					fieldname: ['name', 'title', 'costing'],
				},
			}).then((r) => {
				if (token !== this._costing_poll.token || this._costing_poll.name !== doc_name) return;
				const row = (r && r.message) || {};
				if (!row.costing) return;

				const title = row.title || doc_name;
				this._stop_costing_poll();
				this._fetch_list(true);
				if (this.state.selected === doc_name) {
					this._load_detail(doc_name);
				}
				frappe.show_alert({
					message: __('Costing is ready for {0}.', [title]),
					indicator: 'green',
				}, 5);
				this._sync_alert_container_position();
			}).always(() => {
				if (token === this._costing_poll.token) {
					this._costing_poll.in_flight = false;
				}
			});
		};

		tick();
		this._costing_poll.timer = window.setInterval(tick, this._costing_poll.interval_ms);
	}

	_stop_costing_poll() {
		const poll = this._costing_poll;
		if (!poll) return;
		if (poll.timer) {
			window.clearInterval(poll.timer);
		}
		poll.timer = null;
		poll.name = null;
		poll.attempts = 0;
		poll.in_flight = false;
	}

	_sync_alert_container_position() {
		const $container = $('#alert-container');
		if (!$container.length) return;
		const route = (typeof frappe.get_route === 'function' ? (frappe.get_route() || []) : [])
			.join('/')
			.toLowerCase();
		const pathname = (window.location.pathname || '').toLowerCase();
		const hash = (window.location.hash || '').toLowerCase();
		const is_workbench = route.indexOf('techpack-costing-workbench') !== -1
			|| pathname.indexOf('/app/techpack-costing-workbench') !== -1
			|| hash.indexOf('techpack-costing-workbench') !== -1;
		$container.toggleClass('tcwb-alert-top-right', is_workbench);
		if (is_workbench) {
			$container.css({ top: '20px', bottom: 'auto', right: '20px' });
		} else {
			$container.css({ top: '', bottom: '', right: '' });
		}
	}

	_render_list() {
		const rows = this.state.records;
		if (!rows.length) {
			this.$els.list.html('<div class="tcwb-empty">No records found.</div>');
			return;
		}
		const esc = frappe.utils.escape_html;
		const html = rows.map((row) => {
			const active = row.name === this.state.selected ? ' is-active' : '';
			const title = row.title || row.name;
			const created = this._fmt_date(row.creation);
			const owner = row.owner || '';
			const is_processing = !row.has_costing && row.name === this._costing_poll.name;
			const dot = row.has_costing
				? '<span class="tcwb-dot ok" title="Costing ready"></span>'
				: is_processing
					? '<span class="tcwb-dot processing" title="Costing in progress"></span>'
					: '<span class="tcwb-dot pending" title="Costing pending"></span>';
			return `
				<div class="tcwb-list-item${active}" data-name="${esc(row.name)}">
					<div class="tcwb-list-title">${dot}<span>${esc(title)}</span></div>
					<div class="tcwb-list-meta">
						<span class="tcwb-list-owner" title="${esc(owner)}">${esc(this._short_owner(owner))}</span>
						<span class="tcwb-list-sep">·</span>
						<span class="tcwb-list-date">${esc(created)}</span>
					</div>
				</div>
			`;
		}).join('');
		this.$els.list.html(html);

		this.$els.list.find('.tcwb-list-item').on('click', (e) => {
			const name = $(e.currentTarget).attr('data-name');
			this._select_record(name);
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
			<div class="tcwb-pager-info">${from}–${to} of ${total}</div>
			<div class="tcwb-pager-btns">
				<button class="btn btn-default btn-xs" data-role="prev" ${page <= 1 ? 'disabled' : ''}>‹</button>
				<span class="tcwb-pager-page">${page}/${total_pages}</span>
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
		this.state.expanded = {};
		this.$els.list.find('.tcwb-list-item').removeClass('is-active');
		this.$els.list.find(`.tcwb-list-item[data-name="${name}"]`).addClass('is-active');
		this._write_hash();
		this._load_detail(name);
	}

	// ---------- detail panel ----------
	_load_detail(name) {
		this.state.detail_loading = true;
		this.$els.detail.html('<div class="tcwb-detail-empty">Loading…</div>');
		frappe.call({
			method: 'prism.api.techpack_costing.get_record',
			args: { name },
		}).then((r) => {
			this.state.detail_loading = false;
			const res = r.message || {};
			if (!res.status) {
				this.$els.detail.html(`<div class="tcwb-detail-empty error">${frappe.utils.escape_html(res.error || 'Failed to load record.')}</div>`);
				return;
			}
			this._render_detail(res.data || {});
		}).catch((err) => {
			this.state.detail_loading = false;
			this.$els.detail.html(`<div class="tcwb-detail-empty error">${frappe.utils.escape_html((err && err.message) || 'Request failed.')}</div>`);
		});
	}

	_render_detail(data) {
		const esc = frappe.utils.escape_html;
		const costing = data.costing || [];
		const title = data.title || data.name || '';
		const created = this._fmt_date(data.creation);
		const owner = data.owner || '';
		this.state.detail_data = data;
		this.state.saving_adjustments = false;

		costing.forEach((section) => {
			const row = section || {};
			const sdata = row.costing || {};
			const adjustment = this._normalize_adjustment(sdata.adjustment_percent);
			sdata.adjustment_percent = adjustment;
			const effective_base = this._section_effective_base_total({ costing: sdata });
			sdata.adjusted_total_fabric_cost = this._calc_adjusted_total(effective_base, adjustment);
			const kg_per_piece = this._normalize_kg_per_piece(sdata.kg_per_piece);
			sdata.kg_per_piece = kg_per_piece;
			sdata.cost_per_piece = this._calc_cost_per_piece(sdata.adjusted_total_fabric_cost, kg_per_piece);
			row.costing = sdata;
		});
		this.state.original_adjustments = costing.map((section) => this._section_adjustment(section));
		this.state.original_kg_per_piece = costing.map((section) => this._section_kg_per_piece(section));
		this.state.original_adjusted_heads = costing.map((section) => this._snapshot_section_adjusted_heads(section));
		this.state.original_head_remarks = costing.map((section) => this._snapshot_section_head_remarks(section));
		this.state.original_print_types = costing.map((section) => (((section || {}).costing || {}).print_type || 'None'));
		this.state.adjustments_dirty = false;

		this.state.original_trim_unit_prices = {};
		this.state.original_trim_units = {};
		this.state.original_trim_selections = {};
		const tc = data.trim_costing;
		if (tc && Array.isArray(tc.trims)) {
			let trim_total = 0;
			tc.trims.forEach((t) => {
				if (!t || !t.id) return;
				this.state.original_trim_unit_prices[t.id] = this._num(t.unit_price);
				this.state.original_trim_units[t.id] = this._num(t.units);
				t.cost = this._num(t.unit_price) * this._num(t.units);
				if (!this._is_blankish(t.trim_group)) {
					t.is_selected = t.is_selected !== false;
					this.state.original_trim_selections[t.id] = t.is_selected;
				}
				if (t.is_selected !== false) trim_total += t.cost;
			});
			tc.total_trim_cost = trim_total;
		}

		const sections_html = costing.length
			? costing.map((s, idx) => this._render_section(s, idx)).join('')
			: '<div class="tcwb-detail-empty">No costing data is available for this record yet.</div>';

		const fabric_summary_html = costing.length
			? `
				<div class="tcwb-fabric-summary">
					<span class="tcwb-fabric-summary-label">Total Fabric Cost / Piece</span>
					<span class="tcwb-fabric-summary-value" data-role="fabric-grand-cost-per-piece">${this._fmt_currency(this._grand_cost_per_piece(costing))}</span>
				</div>
			`
			: '';

		const trim_panel_html = this._render_trim_panel(data.trim_costing);
		const print_panel_html = this._render_print_panel(data.print_costing);
		const emb_panel_html = this._render_emb_panel(data.embroidery_cost);
		const sam_panel_html = this._render_sam_panel(data.sam_cost);
		const final_rollup_panel_html = this._render_final_rollup_panel(data.final_rollup);

		const tech_pack_link = data.tech_pack
			? `<a class="tcwb-headlink" href="${esc(data.tech_pack)}" target="_blank" rel="noopener">Open Tech Pack ↗</a>`
			: '';
		const artwork_link = data.artwork
			? `<a class="tcwb-headlink" href="${esc(data.artwork)}" target="_blank" rel="noopener">Open Artwork ↗</a>`
			: '';
		const print_consts = this._print_constants();
		if (!data.print_costing) {
			data.print_costing = { prints: [] };
		} else if (!Array.isArray(data.print_costing.prints)) {
			data.print_costing.prints = [];
		}
		// Order quantity is owned by the doc's `order_quantity` field. Normalize it
		// (fall back to the rules default) and mirror it into print_costing so the
		// snapshot / validation / save machinery reads a consistent value.
		const doc_oq = this._num(data.order_quantity) || print_consts.default_order_quantity;
		data.order_quantity = doc_oq;
		data.print_costing.order_quantity = doc_oq;
		this.state.original_print_snapshot = this._print_input_snapshot(data.print_costing);

		if (!data.embroidery_cost) data.embroidery_cost = { sections: [] };
		if (!Array.isArray(data.embroidery_cost.sections)) data.embroidery_cost.sections = [];
		this.state.original_emb_snapshot = this._emb_input_snapshot(data.embroidery_cost);

		if (!data.sam_cost) data.sam_cost = { sam_minutes: 0 };
		this.state.original_sam_snapshot = this._sam_input_snapshot(data.sam_cost);

		if (!data.final_rollup) data.final_rollup = { rejection_percent: 0, testing_percent: 0, profit_percent: 0 };
		if (!data.final_rollup.currency_code) data.final_rollup.currency_code = 'INR';
		if (this._num(data.final_rollup.conversion_factor) <= 0) data.final_rollup.conversion_factor = 1.0;
		this.state.original_final_rollup_snapshot = this._final_rollup_input_snapshot(data.final_rollup);

		const save_btn = '<button class="btn btn-primary btn-sm" data-role="save-adjustments" disabled>Save Changes</button>';

		this.$els.detail.html(`
			<div class="tcwb-detail-inner">
				<header class="tcwb-detail-head">
					<div class="tcwb-detail-titleblock">
						<h2 class="tcwb-detail-title">${esc(title)}</h2>
						<div class="tcwb-detail-meta">
							<span>${esc(this._short_owner(owner))}</span>
							<span class="tcwb-sep">·</span>
							<span>${esc(created)}</span>
						</div>
					</div>
						<div class="tcwb-detail-actions">
							${tech_pack_link}
							${artwork_link}
							${save_btn}
							<button class="btn btn-default btn-sm" data-role="download-pdf" title="Download PDF" aria-label="Download PDF">Download</button>
						</div>
				</header>
				<nav class="tcwb-tabs" role="tablist">
					<button class="tcwb-tab is-active" type="button" role="tab" data-tab="fabric" aria-selected="true">Fabrics</button>
					<button class="tcwb-tab" type="button" role="tab" data-tab="trims" aria-selected="false">Trims</button>
					<button class="tcwb-tab" type="button" role="tab" data-tab="printing" aria-selected="false">Printing</button>
					<button class="tcwb-tab" type="button" role="tab" data-tab="embroidery" aria-selected="false">Embroidery</button>
					<button class="tcwb-tab" type="button" role="tab" data-tab="sam" aria-selected="false">SAM</button>
					<button class="tcwb-tab" type="button" role="tab" data-tab="rollup" aria-selected="false">Final Roll-Up</button>
				</nav>
				<div class="tcwb-tab-content">
					<div class="tcwb-tab-panel is-active" data-tab-panel="fabric" role="tabpanel">
						${fabric_summary_html}
						<div class="tcwb-sections" data-role="sections">${sections_html}</div>
					</div>
					<div class="tcwb-tab-panel" data-tab-panel="trims" role="tabpanel" hidden>
						${trim_panel_html}
					</div>
					<div class="tcwb-tab-panel" data-tab-panel="printing" role="tabpanel" hidden>
						${print_panel_html}
					</div>
					<div class="tcwb-tab-panel" data-tab-panel="embroidery" role="tabpanel" hidden>
						${emb_panel_html}
					</div>
					<div class="tcwb-tab-panel" data-tab-panel="sam" role="tabpanel" hidden>
						${sam_panel_html}
					</div>
					<div class="tcwb-tab-panel" data-tab-panel="rollup" role="tabpanel" hidden>
						${final_rollup_panel_html}
					</div>
				</div>
			</div>
		`);

		this._wire_detail_actions();
		this._refresh_section_header_metrics();
		this._set_save_button_state();
	}

	_wire_detail_actions() {
		const $detail = this.$els.detail;

		$detail.find('[data-role="download-pdf"]').on('click', () => this._download_current_pdf());
		$detail.find('[data-role="save-adjustments"]').on('click', () => this._save_adjustments());

		$detail.find('.tcwb-tab').on('click', (e) => {
			const $tab = $(e.currentTarget);
			const target = $tab.attr('data-tab');
			$detail.find('.tcwb-tab').removeClass('is-active').attr('aria-selected', 'false');
			$tab.addClass('is-active').attr('aria-selected', 'true');
			$detail.find('.tcwb-tab-panel').each((_, el) => {
				const $panel = $(el);
				const active = $panel.attr('data-tab-panel') === target;
				$panel.toggleClass('is-active', active);
				$panel.prop('hidden', !active);
			});
		});

		$detail.off('click.tcwb-section-toggle').on('click.tcwb-section-toggle', '.tcwb-section-toggle', (e) => {
			const $section = $(e.currentTarget).closest('.tcwb-section');
			const open = !$section.hasClass('is-open');
			$section.toggleClass('is-open', open);
			$(e.currentTarget).attr('aria-expanded', open ? 'true' : 'false');
		});

		$detail.off('input.tcwb-kg change.tcwb-kg').on('input.tcwb-kg change.tcwb-kg', '[data-role="kg-per-piece-input"]', (e) => {
			const $input = $(e.currentTarget);
			const idx = parseInt($input.attr('data-idx'), 10);
			const raw = $input.val();
			const kg_val = raw === '' ? '' : (this._num(raw) / 1000);
			this._set_section_kg_per_piece(idx, kg_val);
			this.state.adjustments_dirty = this._has_adjustment_changes();
			this._refresh_section_header_metrics();
			this._set_save_button_state();
		});

		$detail.off('change.tcwb-dyes-pt').on('change.tcwb-dyes-pt', '[data-role="dyes-print-type"]', (e) => {
			const $input = $(e.currentTarget);
			const idx = parseInt($input.attr('data-section-idx'), 10);
			const pt = $input.val();
			this._on_dyes_print_type_change(idx, pt);
		});

		$detail.off('input.tcwb-adj change.tcwb-adj').on('input.tcwb-adj change.tcwb-adj', '[data-role="adjustment-input"]', (e) => {
			const $input = $(e.currentTarget);
			const idx = parseInt($input.attr('data-idx'), 10);
			const next = this._normalize_adjustment($input.val());
			this._set_section_adjustment(idx, next);
			this.state.adjustments_dirty = this._has_adjustment_changes();
			this._refresh_section_header_metrics();
			this._set_save_button_state();
		});

		$detail.off('input.tcwb-calc-adj change.tcwb-calc-adj').on('input.tcwb-calc-adj change.tcwb-calc-adj', '.tcwb-calc-adj-input', (e) => {
			const $input = $(e.currentTarget);
			const idx = parseInt($input.attr('data-section-idx'), 10);
			const head_key = $input.attr('data-head-key');
			this._set_section_head_adjusted(idx, head_key, $input.val());

			$input.removeClass('is-pos is-neg');
			const val = $input.val();
			if (val !== '') {
				const section = this._get_costing_sections()[idx] || {};
				const sys = this._num(((((section.costing || {}).cost_per_kg || {}).breakup || {})[head_key] || {}).cost_per_kg);
				const num = this._num(val);
				if (Math.abs(num - sys) >= 0.0001) {
					$input.addClass(num > sys ? 'is-pos' : 'is-neg');
				}
			}

			this._refresh_section_calc(idx);
			this._refresh_section_header_metrics();
			this._set_save_button_state();
		});

		$detail.off('focusout.tcwb-calc-adj').on('focusout.tcwb-calc-adj', '.tcwb-calc-adj-input', (e) => {
			const $input = $(e.currentTarget);
			const val = $input.val();
			if (val.trim() !== '') {
				$input.val(this._num(val).toFixed(2));
			}
		});

		$detail.off('input.tcwb-calc-remark change.tcwb-calc-remark').on('input.tcwb-calc-remark change.tcwb-calc-remark', '.tcwb-calc-remark-input', (e) => {
			const $input = $(e.currentTarget);
			const idx = parseInt($input.attr('data-section-idx'), 10);
			const head_key = $input.attr('data-head-key');
			this._set_section_head_remark(idx, head_key, $input.val());
			this._set_save_button_state();
		});

		$detail.find('.tcwb-trim-checkbox').on('change', (e) => {
			const $cb = $(e.currentTarget);
			const id = $cb.attr('data-trim-id');
			const checked = $cb.is(':checked');
			this._set_trim_selection(id, checked);
			$detail.find(`.tcwb-trim-unit-price-input[data-trim-id="${id}"]`).prop('disabled', !checked);
			$detail.find(`.tcwb-trim-units-input[data-trim-id="${id}"]`).prop('disabled', !checked);
			this._refresh_trim_total();
			this._set_save_button_state();
		});

		$detail.find('.tcwb-trim-unit-price-input').on('input change', (e) => {
			const $input = $(e.currentTarget);
			const id = $input.attr('data-trim-id');
			this._set_trim_unit_price(id, $input.val());
			const orig = this._num($input.attr('data-orig-unit-price'));
			const cur = this._num($input.val());
			const cls = this._trim_cost_delta_class(cur, orig);
			$input.removeClass('is-pos is-neg');
			if (cls) $input.addClass(cls);
			this._refresh_trim_row(id);
			this._refresh_trim_total();
			this._set_save_button_state();
		});

		$detail.find('.tcwb-trim-unit-price-input').on('blur', (e) => {
			const $input = $(e.currentTarget);
			const val = $input.val();
			if (val.trim() !== '') {
				$input.val(this._num(val).toFixed(2));
			}
		});

		$detail.find('.tcwb-trim-units-input').on('input change', (e) => {
			const $input = $(e.currentTarget);
			const id = $input.attr('data-trim-id');
			this._set_trim_units(id, $input.val());
			const orig = this._num($input.attr('data-orig-units'));
			const cur = this._num($input.val());
			const cls = this._trim_cost_delta_class(cur, orig);
			$input.removeClass('is-pos is-neg');
			if (cls) $input.addClass(cls);
			this._refresh_trim_row(id);
			this._refresh_trim_total();
			this._set_save_button_state();
		});

		this._wire_print_panel();
		this._wire_emb_panel();
		this._wire_sam_panel();
		this._wire_final_rollup_panel();

		$detail.find('.tcwb-img').on('error', (e) => {
			$(e.currentTarget).closest('.tcwb-fabric-image').remove();
		});
	}

	_save_adjustments() {
		const name = this.state.selected;
		if (!name || this.state.saving_adjustments) return;

		const has_adj = this._has_adjustment_changes();
		const has_trims = this._has_trim_cost_changes();
		const has_prints = this._has_print_changes();
		const has_emb = this._has_emb_changes();
		const has_sam = this._has_sam_changes();
		const has_rollup = this._has_final_rollup_changes();
		if (!has_adj && !has_trims && !has_prints && !has_emb && !has_sam && !has_rollup) return;

		if (has_prints) {
			const v = this._validate_print_costing();
			if (!v.valid) {
				this._apply_print_validation_ui(v.invalid_fields);
				this.$els.detail.find('.tcwb-tab[data-tab="printing"]').trigger('click');
				frappe.msgprint({
					title: __('Validation Error'),
					message: v.errors.map((m) => frappe.utils.escape_html(m)).join('<br>'),
					indicator: 'red',
				});
				return;
			}
		}

		if (has_emb) {
			const v = this._validate_emb_cost();
			if (!v.valid) {
				this._apply_emb_validation_ui(v.invalid_fields);
				this.$els.detail.find('.tcwb-tab[data-tab="embroidery"]').trigger('click');
				frappe.msgprint({
					title: __('Validation Error'),
					message: v.errors.map((m) => frappe.utils.escape_html(m)).join('<br>'),
					indicator: 'red',
				});
				return;
			}
		}

		if (has_sam) {
			const v = this._validate_sam_cost();
			if (!v.valid) {
				this._apply_sam_validation_ui(v.invalid_fields);
				this.$els.detail.find('.tcwb-tab[data-tab="sam"]').trigger('click');
				frappe.msgprint({
					title: __('Validation Error'),
					message: v.errors.map((m) => frappe.utils.escape_html(m)).join('<br>'),
					indicator: 'red',
				});
				return;
			}
		}

		if (has_rollup) {
			const v = this._validate_final_rollup();
			if (!v.valid) {
				this._apply_final_rollup_validation_ui(v.invalid_fields);
				this.$els.detail.find('.tcwb-tab[data-tab="rollup"]').trigger('click');
				frappe.msgprint({
					title: __('Validation Error'),
					message: v.errors.map((m) => frappe.utils.escape_html(m)).join('<br>'),
					indicator: 'red',
				});
				return;
			}
		}

		const data = this.state.detail_data || {};
		const calls = [];

		if (has_adj) {
			const adj_payload = (data.costing || []).map((section, idx) => ({
				idx,
				adjustment_percent: this._section_adjustment(section),
				kg_per_piece: this._section_kg_per_piece(section),
				adjusted_heads: this._snapshot_section_adjusted_heads(section),
				head_remarks: this._snapshot_section_head_remarks(section),
				print_type: ((section.costing || {}).print_type || 'None'),
			}));
			calls.push({
				kind: 'adjustments',
				promise: frappe.call({
					method: 'prism.api.techpack_costing.save_section_adjustments',
					args: { name, adjustments: JSON.stringify(adj_payload) },
				}),
			});
		}

		if (has_trims) {
			const trims = ((data.trim_costing || {}).trims) || [];
			const trim_payload = trims
				.filter((t) => t && t.id)
				.map((t) => {
					const item = {
						id: t.id,
						unit_price: this._num(t.unit_price),
						units: this._num(t.units),
					};
					if (!this._is_blankish(t.trim_group)) {
						item.is_selected = !!t.is_selected;
					}
					return item;
				});
			calls.push({
				kind: 'trims',
				promise: frappe.call({
					method: 'prism.api.techpack_costing.save_trim_costing',
					args: { name, trim_updates: JSON.stringify(trim_payload) },
				}),
			});
		}

		if (has_prints) {
			const pc = data.print_costing || { prints: [] };
			const print_payload = {
				order_quantity: this._order_quantity(),
				prints: (pc.prints || []).map((s) => ({
					id: s.id,
					print_position: s.print_position || '',
					print_type: s.print_type || '',
					no_of_prints: this._num(s.no_of_prints),
					length: this._num(s.length),
					width: this._num(s.width),
					coverage: this._is_blankish(s.coverage) ? this._print_constants().default_coverage : this._num(s.coverage),
				})),
			};
			calls.push({
				kind: 'prints',
				promise: frappe.call({
					method: 'prism.api.techpack_costing.save_print_costing',
					args: { name, print_costing: JSON.stringify(print_payload) },
				}),
			});
			// Persist the order quantity onto the doc's own field (source of truth).
			calls.push({
				kind: 'order_quantity',
				promise: frappe.call({
					method: 'prism.api.techpack_costing.save_order_quantity',
					args: { name, order_quantity: this._order_quantity() },
				}),
			});
		}

		if (has_emb) {
			const ec = data.embroidery_cost || { sections: [] };
			const emb_payload = {
				sections: (ec.sections || []).map((s) => ({
					id: s.id,
					section_name: s.section_name || '',
					thread_type: s.thread_type || '',
					emb_type: s.emb_type || '',
					no_of_stitches: this._num(s.no_of_stitches),
					no_of_thread_colours: this._num(s.no_of_thread_colours),
					needle_thread_avg: this._num(s.needle_thread_avg),
					letter_design_for_laser: this._num(s.letter_design_for_laser),
					length: this._num(s.length),
					width: this._num(s.width),
				})),
			};
			calls.push({
				kind: 'embroidery',
				promise: frappe.call({
					method: 'prism.api.techpack_costing.save_embroidery_cost',
					args: { name, embroidery_cost: JSON.stringify(emb_payload) },
				}),
			});
		}

		if (has_sam) {
			const sc = data.sam_cost || {};
			const sam_payload = {
				sam_minutes: this._num(sc.sam_minutes),
			};
			calls.push({
				kind: 'sam',
				promise: frappe.call({
					method: 'prism.api.techpack_costing.save_sam_cost',
					args: { name, sam_cost: JSON.stringify(sam_payload) },
				}),
			});
		}

		if (has_rollup) {
			const fr = data.final_rollup || {};
			const components = this._compute_base_costs();
			const fr_payload = {
				fabric_cost: components.fabric_cost,
				trim_cost: components.trim_cost,
				print_cost: components.print_cost,
				embroidery_cost: components.embroidery_cost,
				sam_cost: components.sam_cost,
				rejection_percent: this._num(fr.rejection_percent),
				testing_percent: this._num(fr.testing_percent),
				profit_percent: this._num(fr.profit_percent),
				currency_code: fr.currency_code || 'INR',
				conversion_factor: this._num(fr.conversion_factor) || 1,
			};
			calls.push({
				kind: 'final_rollup',
				promise: frappe.call({
					method: 'prism.api.techpack_costing.save_final_rollup',
					args: { name, final_rollup: JSON.stringify(fr_payload) },
				}),
			});
		}

		this.state.saving_adjustments = true;
		this._set_save_button_state();

		const active_tab = this.$els.detail.find('.tcwb-tab.is-active').attr('data-tab') || 'fabric';

		Promise.all(calls.map((c) => c.promise)).then((results) => {
			let errored = false;
			results.forEach((r, i) => {
				const res = (r && r.message) || {};
				if (!res.status) {
					errored = true;
					frappe.msgprint({ title: __('Error'), message: __(res.error || 'Failed to save changes.'), indicator: 'red' });
					return;
				}
				const kind = calls[i].kind;
				if (kind === 'adjustments' && res.data && Array.isArray(res.data.costing)) {
					this.state.detail_data.costing = res.data.costing;
				}
				if (kind === 'trims' && res.data && res.data.trim_costing) {
					this.state.detail_data.trim_costing = res.data.trim_costing;
				}
				if (kind === 'prints' && res.data && res.data.print_costing) {
					this.state.detail_data.print_costing = res.data.print_costing;
				}
				if (kind === 'order_quantity' && res.data && res.data.order_quantity != null) {
					this.state.detail_data.order_quantity = res.data.order_quantity;
				}
				if (kind === 'embroidery' && res.data && res.data.embroidery_cost) {
					this.state.detail_data.embroidery_cost = res.data.embroidery_cost;
				}
				if (kind === 'sam' && res.data && res.data.sam_cost) {
					this.state.detail_data.sam_cost = res.data.sam_cost;
				}
				if (kind === 'final_rollup' && res.data && res.data.final_rollup) {
					this.state.detail_data.final_rollup = res.data.final_rollup;
				}
			});

			if (!errored) {
				frappe.show_alert({ message: __('Changes saved.'), indicator: 'green' }, 4);
				this._sync_alert_container_position();
			}

			this._render_detail(this.state.detail_data);
			this.$els.detail.find(`.tcwb-tab[data-tab="${active_tab}"]`).trigger('click');
		}).always(() => {
			this.state.saving_adjustments = false;
			this._set_save_button_state();
		});
	}

	_get_trim(id) {
		const data = this.state.detail_data || {};
		const trims = ((data.trim_costing || {}).trims) || [];
		return trims.find((t) => t && t.id === id) || null;
	}

	_set_trim_unit_price(id, value) {
		const t = this._get_trim(id);
		if (!t) return;
		t.unit_price = this._num(value);
		t.cost = this._num(t.unit_price) * this._num(t.units);
	}

	_set_trim_units(id, value) {
		const t = this._get_trim(id);
		if (!t) return;
		t.units = this._num(value);
		t.cost = this._num(t.unit_price) * this._num(t.units);
	}

	_set_trim_selection(id, selected) {
		const t = this._get_trim(id);
		if (!t) return;
		t.is_selected = !!selected;
	}

	_refresh_trim_total() {
		const data = this.state.detail_data || {};
		const tc = data.trim_costing;
		if (!tc) return;
		const trims = tc.trims || [];
		let total = 0;
		for (const t of trims) {
			if (!t) continue;
			if (t.is_selected === false) continue;
			total += this._num(t.cost);
		}
		tc.total_trim_cost = total;
		this.$els.detail.find('.tcwb-trim-summary-value').text(this._fmt_currency(total));
		this._recompute_final_rollup_panel();
	}

	_refresh_trim_row(id) {
		const t = this._get_trim(id);
		if (!t) return;
		const $detail = this.$els.detail;
		const orig_unit_price = this._num((this.state.original_trim_unit_prices || {})[id]);
		const orig_units = this._num((this.state.original_trim_units || {})[id]);
		const orig_cost = orig_unit_price * orig_units;
		const cur_cost = this._num(t.unit_price) * this._num(t.units);
		const cls = this._trim_cost_delta_class(cur_cost, orig_cost);
		const $cost = $detail.find(`[data-trim-cost-id="${id}"]`);
		$cost.text(this._fmt_currency(cur_cost));
		$cost.removeClass('is-pos is-neg');
		if (cls) $cost.addClass(cls);
	}

	_trim_cost_delta_class(current, original) {
		const c = this._num(current);
		const o = this._num(original);
		if (Math.abs(c - o) < 0.0001) return '';
		return c > o ? 'is-pos' : 'is-neg';
	}

	_has_trim_cost_changes() {
		const data = this.state.detail_data || {};
		const trims = ((data.trim_costing || {}).trims) || [];
		const orig_up = this.state.original_trim_unit_prices || {};
		const orig_un = this.state.original_trim_units || {};
		const orig_sel = this.state.original_trim_selections || {};
		for (const t of trims) {
			if (!t || !t.id) continue;
			if (t.id in orig_up) {
				if (Math.abs(this._num(t.unit_price) - this._num(orig_up[t.id])) > 0.0001) return true;
			}
			if (t.id in orig_un) {
				if (Math.abs(this._num(t.units) - this._num(orig_un[t.id])) > 0.0001) return true;
			}
			if (t.id in orig_sel) {
				if (!!t.is_selected !== !!orig_sel[t.id]) return true;
			}
		}
		return false;
	}

	// ---------- printing ----------
	_get_print_options() {
		const data = this.state.detail_data || {};
		if (Array.isArray(data.print_options) && data.print_options.length) {
			return data.print_options;
		}
		return [];
	}

	_get_print_costing_rules() {
		const data = this.state.detail_data || {};
		const rules = data.print_costing_rules || {};
		const extra = this._num(rules.extra_percent);
		const rejection = this._num(rules.rejection_percent);
		const mesh = this._num(rules.mesh_cost_per_screen);
		const coverage = this._num(rules.default_coverage_percent);
		const order_qty = this._num(rules.default_order_quantity);
		return {
			extra_percent: extra,
			rejection_percent: rejection,
			mesh_cost_per_screen: mesh,
			default_coverage_percent: coverage,
			default_order_quantity: order_qty,
		};
	}

	_print_constants() {
		const r = this._get_print_costing_rules();
		return {
			mesh_cost_per_screen: r.mesh_cost_per_screen,
			default_coverage: r.default_coverage_percent,
			default_order_quantity: r.default_order_quantity,
		};
	}

	_order_quantity() {
		// Order quantity is owned by the doc's `order_quantity` field, not the
		// "Print Costing Rules" doctype. Fall back to the rules default only when
		// the doc field is blank/invalid.
		const data = this.state.detail_data || {};
		return this._num(data.order_quantity) || this._print_constants().default_order_quantity;
	}

	_print_rate_for(type) {
		const opts = this._get_print_options();
		return opts.find((o) => o.print_type === type) || null;
	}

	_compute_print_section(section, order_quantity) {
		const consts = this._print_constants();
		const rate = this._print_rate_for(section && section.print_type) || {};
		const rules = this._get_print_costing_rules();
		const no_of_prints = this._num(section.no_of_prints);
		const length = this._num(section.length);
		const width = this._num(section.width);
		const coverage = this._is_blankish(section.coverage) ? consts.default_coverage : this._num(section.coverage);

		const cost_per_inch = this._num(rate.cost_per_inch);
		const manpower_cost = this._num(rate.manpower_cost);
		const extra_percent = rules.extra_percent;
		const rejection_percent = rules.rejection_percent;

		const area = length * width;
		const ink_cost = cost_per_inch * area * (no_of_prints + 1) * (coverage / 100);
		const oq = Math.max(1, this._num(order_quantity) || consts.default_order_quantity);
		const mesh_cost_per_garment = (no_of_prints * consts.mesh_cost_per_screen) / oq;
		const material_cost = (ink_cost + mesh_cost_per_garment) * (1 + extra_percent / 100);
		const gross_total_cost = material_cost + manpower_cost;
		const rejection_amount = gross_total_cost * (rejection_percent / 100);
		const final_cost = section.print_type ? Math.ceil(gross_total_cost + rejection_amount) : 0;

		return {
			cost_per_inch, manpower_cost, extra_percent, rejection_percent,
			area, ink_cost, mesh_cost_per_garment, material_cost,
			gross_total_cost, rejection_amount, final_cost,
		};
	}

	_print_input_snapshot(pc) {
		if (!pc) return JSON.stringify({ oq: null, p: [] });
		const prints = Array.isArray(pc.prints) ? pc.prints : [];
		return JSON.stringify({
			oq: this._num(pc.order_quantity),
			p: prints.map((s) => ({
				pp: (s.print_position || '').trim(),
				pt: s.print_type || '',
				np: this._num(s.no_of_prints),
				l: this._num(s.length),
				w: this._num(s.width),
				c: this._is_blankish(s.coverage) ? null : this._num(s.coverage),
			})),
		});
	}

	_has_print_changes() {
		const current = this._print_input_snapshot(this.state.detail_data && this.state.detail_data.print_costing);
		return current !== (this.state.original_print_snapshot || JSON.stringify({ oq: null, p: [] }));
	}

	_validate_print_costing() {
		const data = this.state.detail_data || {};
		const pc = data.print_costing || {};
		const errors = [];
		const invalid_fields = [];

		const oq = this._num(pc.order_quantity);
		if (!oq || oq <= 0) {
			errors.push('Order Quantity must be greater than 0.');
			invalid_fields.push({ section_id: null, field: 'order_quantity' });
		}

		const options = new Set(this._get_print_options().map((o) => o.print_type));
		const prints = Array.isArray(pc.prints) ? pc.prints : [];
		prints.forEach((sec, idx) => {
			const label = `Section ${idx + 1}`;
			const sid = sec && sec.id;
			if (this._is_blankish(sec && sec.print_position)) {
				errors.push(`${label}: Print Position is required.`);
				invalid_fields.push({ section_id: sid, field: 'print_position' });
			}
			const pt = (sec && sec.print_type) || '';
			if (!pt) {
				errors.push(`${label}: Print Type is required.`);
				invalid_fields.push({ section_id: sid, field: 'print_type' });
			} else if (!options.has(pt)) {
				errors.push(`${label}: Print Type '${pt}' is not a recognized option.`);
				invalid_fields.push({ section_id: sid, field: 'print_type' });
			}
			[
				['no_of_prints', 'No. of Prints'],
				['length', 'Length'],
				['width', 'Width'],
				['coverage', 'Coverage'],
			].forEach(([f, fname]) => {
				if (this._num(sec && sec[f]) <= 0) {
					errors.push(`${label}: ${fname} must be greater than 0.`);
					invalid_fields.push({ section_id: sid, field: f });
				}
			});
		});

		return { valid: errors.length === 0, errors, invalid_fields };
	}

	_apply_print_validation_ui(invalid_fields) {
		const $panel = this.$els.detail.find('[data-tab-panel="printing"]');
		$panel.find('.has-error').removeClass('has-error');
		(invalid_fields || []).forEach(({ section_id, field }) => {
			if (section_id === null) {
				if (field === 'order_quantity') {
					$panel.find('.tcwb-print-oq-input').addClass('has-error');
				}
			} else {
				const $sec = $panel.find(`.tcwb-print-section[data-print-id="${section_id}"]`);
				$sec.find(`[data-field="${field}"]`).addClass('has-error');
			}
		});
	}

	_new_print_section_id() {
		return 'p_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
	}

	_render_print_panel(print_costing, open_ids = null) {
		const consts = this._print_constants();
		const pc = print_costing || { prints: [] };
		const order_quantity = this._order_quantity();
		const prints = Array.isArray(pc.prints) ? pc.prints : [];

		let grand_total = 0;
		prints.forEach((s) => { grand_total += this._compute_print_section(s, order_quantity).final_cost; });

		const sections_html = prints.length
			? prints.map((s, idx) => this._render_print_section(s, idx, order_quantity, open_ids)).join('')
			: '<div class="tcwb-empty">No print sections yet. Add one to start.</div>';

		const rules = this._get_print_costing_rules();
		return `
			<div class="tcwb-print-wrap">
				<div class="tcwb-print-header">
					<div class="tcwb-print-header-left">
						<label class="tcwb-print-oq">
							<span class="tcwb-print-oq-label">Order Quantity <span class="tcwb-required">*</span></span>
							<input type="number" required min="1" step="1" class="form-control input-xs tcwb-print-oq-input" value="${order_quantity}">
						</label>
						<div class="tcwb-print-rules" title="Configured in 'Print Costing Rules'">
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Extra</span>
								<span class="tcwb-print-rule-value">${this._fmt_percent(rules.extra_percent)}</span>
							</div>
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Rejection</span>
								<span class="tcwb-print-rule-value">${this._fmt_percent(rules.rejection_percent)}</span>
							</div>
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Mesh / Screen</span>
								<span class="tcwb-print-rule-value">${this._fmt_currency(rules.mesh_cost_per_screen)}</span>
							</div>
						</div>
					</div>
					<div class="tcwb-print-header-right">
						<div class="tcwb-print-grand">
							<span class="tcwb-print-grand-label">Print Cost / Garment</span>
							<span class="tcwb-print-grand-value" data-role="print-grand-total">${this._fmt_currency(grand_total)}</span>
						</div>
					</div>
				</div>
				<div class="tcwb-print-add">
					<button class="btn btn-default btn-xs" type="button" data-role="add-print-section">+ Add Print Section</button>
				</div>
				<div class="tcwb-print-sections" data-role="print-sections">${sections_html}</div>
			</div>
		`;
	}

	_render_print_section(section, idx, order_quantity, open_ids = null) {
		const esc = frappe.utils.escape_html;
		const consts = this._print_constants();
		const sec = section || {};
		const id = sec.id || '';
		const computed = this._compute_print_section(sec, order_quantity);

		const options = this._get_print_options();
		const type_options = ['<option value="">— Select —</option>'].concat(
			options.map((o) => {
				const selected = o.print_type === sec.print_type ? ' selected' : '';
				return `<option value="${esc(o.print_type)}"${selected}>${esc(o.print_type)}</option>`;
			})
		).join('');

		const coverage_val = this._is_blankish(sec.coverage) ? consts.default_coverage : this._num(sec.coverage);
		const title = sec.print_position ? esc(sec.print_position) : `Print ${idx + 1}`;
		const open = open_ids ? open_ids.has(id) : (idx === 0);

		return `
			<div class="tcwb-print-section${open ? ' is-open' : ''}" data-print-id="${esc(id)}">
				<div class="tcwb-print-section-head">
					<button class="tcwb-print-section-toggle" type="button" aria-expanded="${open ? 'true' : 'false'}">
						<span class="tcwb-section-caret">›</span>
						<span class="tcwb-print-section-title">${title}</span>
					</button>
					<span class="tcwb-print-section-final">
						<span class="tcwb-print-section-final-label">Section Cost</span>
						<span class="tcwb-print-section-final-value" data-role="print-section-final">${this._fmt_currency(computed.final_cost)}</span>
					</span>
					<button class="btn btn-default btn-xs tcwb-print-remove" type="button" data-role="remove-print-section" title="Remove">×</button>
				</div>
				<div class="tcwb-print-section-body">
					<div class="tcwb-print-grid tcwb-print-grid-inputs">
						<label class="tcwb-print-field">
							<span>Print Position <span class="tcwb-required">*</span></span>
							<input type="text" required class="form-control input-xs" data-field="print_position" value="${esc(sec.print_position || '')}" placeholder="Chest, Back, Sleeve…">
						</label>
						<label class="tcwb-print-field">
							<span>Print Type <span class="tcwb-required">*</span></span>
							<select required class="form-control input-xs" data-field="print_type">${type_options}</select>
						</label>
						<label class="tcwb-print-field">
							<span>No. of Prints <span class="tcwb-required">*</span></span>
							<input type="number" required min="1" step="1" class="form-control input-xs" data-field="no_of_prints" value="${this._num(sec.no_of_prints) || ''}">
						</label>
						<label class="tcwb-print-field">
							<span>Length (in) <span class="tcwb-required">*</span></span>
							<input type="number" required min="0.01" step="0.01" class="form-control input-xs" data-field="length" value="${this._num(sec.length) || ''}">
						</label>
						<label class="tcwb-print-field">
							<span>Width (in) <span class="tcwb-required">*</span></span>
							<input type="number" required min="0.01" step="0.01" class="form-control input-xs" data-field="width" value="${this._num(sec.width) || ''}">
						</label>
						<label class="tcwb-print-field">
							<span>Coverage (%) <span class="tcwb-required">*</span></span>
							<input type="number" required min="0.01" max="100" step="1" class="form-control input-xs" data-field="coverage" value="${coverage_val}">
						</label>
					</div>
					<div class="tcwb-print-breakdown">
						<div class="tcwb-print-facts">
							${this._render_print_facts(computed)}
						</div>
						<div class="tcwb-print-calc-wrap">
							${this._render_print_calc(computed)}
						</div>
					</div>
				</div>
			</div>
		`;
	}

	_render_print_facts(computed) {
		const facts = [
			['Area',           `${computed.area.toFixed(2)} sq in`, 'area'],
			['Ink Cost / inch', this._fmt_currency(computed.cost_per_inch), 'cost_per_inch'],
			['Manpower',       this._fmt_currency(computed.manpower_cost), 'manpower_cost'],
			['Ink Cost',       this._fmt_currency(computed.ink_cost), 'ink_cost'],
			['Mesh / garment', this._fmt_currency(computed.mesh_cost_per_garment), 'mesh_cost_per_garment'],
			['Material',       this._fmt_currency(computed.material_cost), 'material_cost'],
			['Gross Total',    this._fmt_currency(computed.gross_total_cost), 'gross_total_cost'],
			['Rejection Amt',  this._fmt_currency(computed.rejection_amount), 'rejection_amount'],
		];
		const esc = frappe.utils.escape_html;
		const rows = facts.map(([label, value, key]) => `
			<div class="tcwb-fact">
				<dt>${esc(label)}</dt>
				<dd><span data-print-value="${esc(key)}_fact">${value}</span></dd>
			</div>
		`).join('');
		return `<dl class="tcwb-facts">${rows}</dl>`;
	}

	_render_print_calc(computed) {
		const calc_row = (op, label, value_key, value_text, extra_cls = '') => `
			<div class="tcwb-calc-row${extra_cls ? ' ' + extra_cls : ''}">
				<span class="tcwb-calc-op">${op}</span>
				<span class="tcwb-calc-label">${label}</span>
				<span class="tcwb-calc-amount" data-print-value="${value_key}">${value_text}</span>
			</div>
		`;
		const ext_pct = `<span class="tcwb-calc-pct" data-print-value="extra_percent_label">(incl. ${this._fmt_percent(computed.extra_percent)} extra)</span>`;
		const rej_pct = `<span class="tcwb-calc-pct" data-print-value="rejection_percent_label">(${this._fmt_percent(computed.rejection_percent)})</span>`;
		return `
			<div class="tcwb-calc">
				${calc_row('',  'Ink Cost',         'ink_cost',              this._fmt_currency(computed.ink_cost))}
				${calc_row('+', 'Mesh / garment',   'mesh_cost_per_garment', this._fmt_currency(computed.mesh_cost_per_garment))}
				${calc_row('=', `Material ${ext_pct}`, 'material_cost',      this._fmt_currency(computed.material_cost), 'subtotal')}
				${calc_row('+', 'Manpower',         'manpower_cost',         this._fmt_currency(computed.manpower_cost))}
				${calc_row('=', 'Gross Total',      'gross_total_cost',      this._fmt_currency(computed.gross_total_cost), 'subtotal')}
				${calc_row('+', `Rejection ${rej_pct}`, 'rejection_amount',  this._fmt_currency(computed.rejection_amount), 'warning')}
				${calc_row('=', 'Final / garment',  'final_cost',            this._fmt_currency(computed.final_cost), 'total')}
			</div>
		`;
	}

	_get_print_state() {
		const data = this.state.detail_data || {};
		if (!data.print_costing) {
			data.print_costing = { prints: [] };
			this.state.detail_data = data;
		}
		if (!Array.isArray(data.print_costing.prints)) data.print_costing.prints = [];
		// Keep print_costing's order_quantity mirrored to the doc field (source of truth).
		data.print_costing.order_quantity = this._order_quantity();
		return data.print_costing;
	}

	_recompute_print_panel() {
		const pc = this._get_print_state();
		const order_quantity = this._order_quantity();
		let grand_total = 0;
		const $panel = this.$els.detail.find('[data-tab-panel="printing"]');

		(pc.prints || []).forEach((sec) => {
			const computed = this._compute_print_section(sec, order_quantity);
			grand_total += computed.final_cost;
			const $sec = $panel.find(`.tcwb-print-section[data-print-id="${sec.id}"]`);
			if (!$sec.length) return;
			$sec.find('[data-role="print-section-final"]').text(this._fmt_currency(computed.final_cost));

			const setValue = (key, value) => {
				$sec.find(`[data-print-value="${key}"]`).text(value);
				$sec.find(`[data-print-value="${key}_fact"]`).text(value);
			};
			setValue('area', `${computed.area.toFixed(2)} sq in`);
			setValue('cost_per_inch', this._fmt_currency(computed.cost_per_inch));
			setValue('manpower_cost', this._fmt_currency(computed.manpower_cost));
			setValue('extra_percent', this._fmt_percent(computed.extra_percent));
			setValue('rejection_percent', this._fmt_percent(computed.rejection_percent));
			setValue('ink_cost', this._fmt_currency(computed.ink_cost));
			setValue('mesh_cost_per_garment', this._fmt_currency(computed.mesh_cost_per_garment));
			setValue('material_cost', this._fmt_currency(computed.material_cost));
			setValue('gross_total_cost', this._fmt_currency(computed.gross_total_cost));
			setValue('rejection_amount', this._fmt_currency(computed.rejection_amount));
			setValue('final_cost', this._fmt_currency(computed.final_cost));
			$sec.find('[data-print-value="extra_percent_label"]').text(`(incl. ${this._fmt_percent(computed.extra_percent)} extra)`);
			$sec.find('[data-print-value="rejection_percent_label"]').text(`(${this._fmt_percent(computed.rejection_percent)})`);
		});

		$panel.find('[data-role="print-grand-total"]').text(this._fmt_currency(grand_total));
		pc.total_print_cost = grand_total;
		this._recompute_final_rollup_panel();
	}

	_rerender_print_panel() {
		const $panel = this.$els.detail.find('[data-tab-panel="printing"]');
		const open_ids = new Set();
		$panel.find('.tcwb-print-section.is-open').each((_, el) => {
			const id = $(el).attr('data-print-id');
			if (id) open_ids.add(id);
		});
		$panel.html(this._render_print_panel(this.state.detail_data && this.state.detail_data.print_costing, open_ids));
		this._wire_print_panel();
	}

	_wire_print_panel() {
		const $panel = this.$els.detail.find('[data-tab-panel="printing"]');
		if (!$panel.length) return;

		$panel.find('.tcwb-print-oq-input').off('input.tcwb-print').on('input.tcwb-print', (e) => {
			const $input = $(e.currentTarget);
			// Order quantity is stored on the doc's `order_quantity` field. Only kept
			// in memory here; persisted to the doc on "Save Changes".
			(this.state.detail_data || {}).order_quantity = this._num($input.val());
			$input.removeClass('has-error');
			this._recompute_print_panel();
			this._set_save_button_state();
		});

		$panel.find('[data-role="add-print-section"]').off('click.tcwb-print').on('click.tcwb-print', () => {
			const pc = this._get_print_state();
			const new_id = this._new_print_section_id();
			pc.prints.push({
				id: new_id,
				print_position: '',
				print_type: '',
				no_of_prints: 0,
				length: 0,
				width: 0,
				coverage: this._print_constants().default_coverage,
			});
			this._rerender_print_panel();
			this._set_save_button_state();

			const $new_sec = this.$els.detail.find(`.tcwb-print-section[data-print-id="${new_id}"]`);
			if ($new_sec.length) {
				$new_sec.addClass('is-open');
				$new_sec.find('.tcwb-print-section-toggle').attr('aria-expanded', 'true');
				$new_sec[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
				const $position_input = $new_sec.find('[data-field="print_position"]');
				if ($position_input.length) {
					setTimeout(() => $position_input.trigger('focus'), 0);
				}
			}
		});

		$panel.find('.tcwb-print-section-toggle').off('click.tcwb-print').on('click.tcwb-print', (e) => {
			const $btn = $(e.currentTarget);
			const $section = $btn.closest('.tcwb-print-section');
			const open = !$section.hasClass('is-open');
			$section.toggleClass('is-open', open);
			$btn.attr('aria-expanded', open ? 'true' : 'false');
		});

		$panel.find('[data-role="remove-print-section"]').off('click.tcwb-print').on('click.tcwb-print', (e) => {
			const $sec = $(e.currentTarget).closest('.tcwb-print-section');
			const id = $sec.attr('data-print-id');
			const pc = this._get_print_state();
			const sec = (pc.prints || []).find((s) => s.id === id);
			const idx = (pc.prints || []).indexOf(sec);
			const label = (sec && sec.print_position) || `Print ${idx + 1}`;
			const esc = frappe.utils.escape_html;
			frappe.confirm(
				__('Remove print section <b>{0}</b>?', [esc(label)]),
				() => {
					pc.prints = (pc.prints || []).filter((s) => s.id !== id);
					this._rerender_print_panel();
					this._set_save_button_state();
				}
			);
		});

		$panel.find('.tcwb-print-section [data-field]').off('input.tcwb-print change.tcwb-print').on('input.tcwb-print change.tcwb-print', (e) => {
			const $input = $(e.currentTarget);
			const $sec = $input.closest('.tcwb-print-section');
			const id = $sec.attr('data-print-id');
			const field = $input.attr('data-field');
			const pc = this._get_print_state();
			const sec = (pc.prints || []).find((s) => s.id === id);
			if (!sec) return;

			let val = $input.val();
			if (field === 'print_position' || field === 'print_type') {
				sec[field] = val;
				if (field === 'print_position') {
					$sec.find('.tcwb-print-section-title').text(val || `Print ${(pc.prints || []).indexOf(sec) + 1}`);
				}
			} else {
				sec[field] = this._num(val);
			}
			$input.removeClass('has-error');
			this._recompute_print_panel();
			this._set_save_button_state();
		});
	}

	// ---------- embroidery ----------
	_get_embroidery_costing_rules() {
		const data = this.state.detail_data || {};
		const r = data.embroidery_costing_rules || {};
		return {
			canvas_cost_per_sq_inch: this._num(r.canvas_cost_per_sq_inch) || 0.0004,
			canvas_layers: this._num(r.canvas_layers) || 3,
			manpower_cost: this._num(r.manpower_cost) || 4,
			overhead_percent: this._num(r.overhead_percent) || 5,
			rejection_percent: this._num(r.rejection_percent) || 10,
			laser_cost_per_letter: this._num(r.laser_cost_per_letter) || 2,
		};
	}

	_get_embroidery_thread_options() {
		const data = this.state.detail_data || {};
		return Array.isArray(data.embroidery_thread_options) ? data.embroidery_thread_options : [];
	}

	_get_embroidery_type_options() {
		const data = this.state.detail_data || {};
		return Array.isArray(data.embroidery_type_options) ? data.embroidery_type_options : [];
	}

	_get_embroidery_stitch_rates() {
		const data = this.state.detail_data || {};
		return Array.isArray(data.embroidery_stitch_rates) ? data.embroidery_stitch_rates : [];
	}

	_lookup_emb_stitch_rate(stitches) {
		const n = this._num(stitches);
		const rates = this._get_embroidery_stitch_rates();
		for (const row of rates) {
			const frm = this._num(row.from_stitches);
			const to = row.to_stitches;
			if (to === null || to === undefined || to === '') {
				if (n >= frm) return this._num(row.rate);
			} else if (frm <= n && n <= this._num(to)) {
				return this._num(row.rate);
			}
		}
		return 0;
	}

	_emb_thread_for(thread_type) {
		return this._get_embroidery_thread_options().find((t) => t.thread_type === thread_type) || null;
	}

	_is_emb_applique(emb_type) {
		return (emb_type || '').toLowerCase().includes('applique');
	}

	_compute_emb_section(section) {
		const rules = this._get_embroidery_costing_rules();
		const sec = section || {};
		const thread = this._emb_thread_for(sec.thread_type) || {};
		const needle = this._num(sec.needle_thread_avg);
		const bobbin = needle ? needle / 3 : 0;
		const cost_per_mtr = this._num(thread.cost_per_mtr);
		const thread_cost = (needle + bobbin) * cost_per_mtr;
		const stitches = this._num(sec.no_of_stitches);
		const stitch_rate = this._lookup_emb_stitch_rate(stitches);
		const cost_per_1000st = (stitches / 1000) * stitch_rate;
		const length = this._num(sec.length);
		const width = this._num(sec.width);
		const area = length * width;
		const canvas_cost = area * rules.canvas_cost_per_sq_inch * rules.canvas_layers;
		const is_applique = this._is_emb_applique(sec.emb_type);
		const letters = this._num(sec.letter_design_for_laser);
		const laser_cost = is_applique ? rules.laser_cost_per_letter * letters : 0;
		const cost = thread_cost + cost_per_1000st + canvas_cost + rules.manpower_cost + laser_cost;
		const overhead_amount = cost * (rules.overhead_percent / 100);
		const rejection_amount = (cost + overhead_amount) * (rules.rejection_percent / 100);
		const final_cost = cost + overhead_amount + rejection_amount;

		return {
			rules,
			thread,
			needle, bobbin, cost_per_mtr, thread_cost,
			stitches, stitch_rate, cost_per_1000st,
			length, width, area, canvas_cost,
			is_applique, letters, laser_cost,
			manpower_cost: rules.manpower_cost,
			overhead_percent: rules.overhead_percent,
			rejection_percent: rules.rejection_percent,
			cost, overhead_amount, rejection_amount, final_cost,
		};
	}

	_emb_input_snapshot(ec) {
		if (!ec) return JSON.stringify({ s: [] });
		const sections = Array.isArray(ec.sections) ? ec.sections : [];
		return JSON.stringify({
			s: sections.map((s) => ({
				n: (s.section_name || '').trim(),
				tt: s.thread_type || '',
				et: s.emb_type || '',
				st: this._num(s.no_of_stitches),
				tc: this._num(s.no_of_thread_colours),
				nt: this._num(s.needle_thread_avg),
				ld: this._num(s.letter_design_for_laser),
				l: this._num(s.length),
				w: this._num(s.width),
			})),
		});
	}

	_has_emb_changes() {
		const current = this._emb_input_snapshot(this.state.detail_data && this.state.detail_data.embroidery_cost);
		return current !== (this.state.original_emb_snapshot || JSON.stringify({ s: [] }));
	}

	_get_emb_state() {
		const data = this.state.detail_data || {};
		if (!data.embroidery_cost) {
			data.embroidery_cost = { sections: [] };
			this.state.detail_data = data;
		}
		if (!Array.isArray(data.embroidery_cost.sections)) data.embroidery_cost.sections = [];
		return data.embroidery_cost;
	}

	_new_emb_section_id() {
		return 'e_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
	}

	_validate_emb_cost() {
		const data = this.state.detail_data || {};
		const ec = data.embroidery_cost || {};
		const errors = [];
		const invalid_fields = [];
		const thread_set = new Set(this._get_embroidery_thread_options().map((t) => t.thread_type));
		const type_set = new Set(this._get_embroidery_type_options().map((t) => t.emb_type));
		const sections = Array.isArray(ec.sections) ? ec.sections : [];

		sections.forEach((sec, idx) => {
			const label = `Section ${idx + 1}`;
			const sid = sec && sec.id;
			const tt = (sec && sec.thread_type) || '';
			if (!tt) {
				errors.push(`${label}: Thread Type is required.`);
				invalid_fields.push({ section_id: sid, field: 'thread_type' });
			} else if (thread_set.size && !thread_set.has(tt)) {
				errors.push(`${label}: Thread Type '${tt}' is not a recognized option.`);
				invalid_fields.push({ section_id: sid, field: 'thread_type' });
			}
			const et = (sec && sec.emb_type) || '';
			if (!et) {
				errors.push(`${label}: EMB Type is required.`);
				invalid_fields.push({ section_id: sid, field: 'emb_type' });
			} else if (type_set.size && !type_set.has(et)) {
				errors.push(`${label}: EMB Type '${et}' is not a recognized option.`);
				invalid_fields.push({ section_id: sid, field: 'emb_type' });
			}
			[
				['no_of_stitches', 'No. of Stitches'],
				['needle_thread_avg', 'Needle Thread Avg.'],
				['length', 'Length'],
				['width', 'Width'],
			].forEach(([f, fname]) => {
				if (this._num(sec && sec[f]) <= 0) {
					errors.push(`${label}: ${fname} must be greater than 0.`);
					invalid_fields.push({ section_id: sid, field: f });
				}
			});
		});

		return { valid: errors.length === 0, errors, invalid_fields };
	}

	_apply_emb_validation_ui(invalid_fields) {
		const $panel = this.$els.detail.find('[data-tab-panel="embroidery"]');
		$panel.find('.has-error').removeClass('has-error');
		(invalid_fields || []).forEach(({ section_id, field }) => {
			const $sec = $panel.find(`.tcwb-emb-section[data-emb-id="${section_id}"]`);
			$sec.find(`[data-field="${field}"]`).addClass('has-error');
		});
	}

	_render_emb_panel(embroidery_cost, open_ids = null) {
		const ec = embroidery_cost || { sections: [] };
		const sections = Array.isArray(ec.sections) ? ec.sections : [];
		const rules = this._get_embroidery_costing_rules();

		let grand_total = 0;
		sections.forEach((s) => { grand_total += this._compute_emb_section(s).final_cost; });

		const sections_html = sections.length
			? sections.map((s, idx) => this._render_emb_section(s, idx, open_ids)).join('')
			: '<div class="tcwb-empty">No embroidery sections yet. Add one to start.</div>';

		return `
			<div class="tcwb-emb-wrap">
				<div class="tcwb-emb-header">
					<div class="tcwb-emb-header-left">
						<div class="tcwb-print-rules" title="Configured in 'Embroidery Costing Rules'">
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Canvas / sq in</span>
								<span class="tcwb-print-rule-value">${this._fmt_currency(rules.canvas_cost_per_sq_inch, 4)}</span>
							</div>
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Layers</span>
								<span class="tcwb-print-rule-value">${this._num(rules.canvas_layers).toLocaleString()}</span>
							</div>
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Manpower</span>
								<span class="tcwb-print-rule-value">${this._fmt_currency(rules.manpower_cost)}</span>
							</div>
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Over Head</span>
								<span class="tcwb-print-rule-value">${this._fmt_percent(rules.overhead_percent)}</span>
							</div>
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Rejection</span>
								<span class="tcwb-print-rule-value">${this._fmt_percent(rules.rejection_percent)}</span>
							</div>
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Laser / letter</span>
								<span class="tcwb-print-rule-value">${this._fmt_currency(rules.laser_cost_per_letter)}</span>
							</div>
						</div>
					</div>
					<div class="tcwb-print-header-right">
						<div class="tcwb-print-grand">
							<span class="tcwb-print-grand-label">Embroidery Cost / Garment</span>
							<span class="tcwb-print-grand-value" data-role="emb-grand-total">${this._fmt_currency(grand_total)}</span>
						</div>
					</div>
				</div>
				<div class="tcwb-print-add">
					<button class="btn btn-default btn-xs" type="button" data-role="add-emb-section">+ Add Embroidery Section</button>
				</div>
				<div class="tcwb-emb-sections" data-role="emb-sections">${sections_html}</div>
			</div>
		`;
	}

	_render_emb_section(section, idx, open_ids = null) {
		const esc = frappe.utils.escape_html;
		const sec = section || {};
		const id = sec.id || '';
		const computed = this._compute_emb_section(sec);
		const title = sec.section_name ? esc(sec.section_name) : `Embroidery ${idx + 1}`;
		const open = open_ids ? open_ids.has(id) : (idx === 0);

		const thread_options = ['<option value="">— Select —</option>'].concat(
			this._get_embroidery_thread_options().map((o) => {
				const selected = o.thread_type === sec.thread_type ? ' selected' : '';
				return `<option value="${esc(o.thread_type)}"${selected}>${esc(o.thread_type)}</option>`;
			})
		).join('');
		const type_options = ['<option value="">— Select —</option>'].concat(
			this._get_embroidery_type_options().map((o) => {
				const selected = o.emb_type === sec.emb_type ? ' selected' : '';
				return `<option value="${esc(o.emb_type)}"${selected}>${esc(o.emb_type)}</option>`;
			})
		).join('');

		const applique = computed.is_applique;
		const laser_field = `
			<label class="tcwb-print-field" data-applique-only style="${applique ? '' : 'display:none;'}">
				<span>Letter/Design for Laser <span class="tcwb-required">*</span></span>
				<input type="number" min="0" step="1" class="form-control input-xs" data-field="letter_design_for_laser" value="${this._num(sec.letter_design_for_laser)}">
			</label>
		`;

		return `
			<div class="tcwb-print-section tcwb-emb-section${open ? ' is-open' : ''}" data-emb-id="${esc(id)}">
				<div class="tcwb-print-section-head">
					<button class="tcwb-print-section-toggle" type="button" aria-expanded="${open ? 'true' : 'false'}">
						<span class="tcwb-section-caret">›</span>
						<span class="tcwb-print-section-title">${title}</span>
					</button>
					<span class="tcwb-print-section-final">
						<span class="tcwb-print-section-final-label">Section Cost</span>
						<span class="tcwb-print-section-final-value" data-role="emb-section-final">${this._fmt_currency(computed.final_cost)}</span>
					</span>
					<button class="btn btn-default btn-xs tcwb-print-remove" type="button" data-role="remove-emb-section" title="Remove">×</button>
				</div>
				<div class="tcwb-print-section-body">
					<div class="tcwb-print-grid tcwb-emb-grid-inputs">
						<label class="tcwb-print-field">
							<span>Section Name</span>
							<input type="text" class="form-control input-xs" data-field="section_name" value="${esc(sec.section_name || '')}" placeholder="Chest, Sleeve…">
						</label>
						<label class="tcwb-print-field">
							<span>Thread Type <span class="tcwb-required">*</span></span>
							<select required class="form-control input-xs" data-field="thread_type">${thread_options}</select>
						</label>
						<label class="tcwb-print-field">
							<span>EMB Type <span class="tcwb-required">*</span></span>
							<select required class="form-control input-xs" data-field="emb_type">${type_options}</select>
						</label>
						<label class="tcwb-print-field">
							<span>No. of Stitches <span class="tcwb-required">*</span></span>
							<input type="number" required min="1" step="1" class="form-control input-xs" data-field="no_of_stitches" value="${this._num(sec.no_of_stitches) || ''}">
						</label>
						<label class="tcwb-print-field">
							<span>Needle Thread Avg. <span class="tcwb-required">*</span></span>
							<input type="number" required min="0.01" step="0.01" class="form-control input-xs" data-field="needle_thread_avg" value="${this._num(sec.needle_thread_avg) || ''}">
						</label>
						<label class="tcwb-print-field">
							<span>Length (in) <span class="tcwb-required">*</span></span>
							<input type="number" required min="0.01" step="0.01" class="form-control input-xs" data-field="length" value="${this._num(sec.length) || ''}">
						</label>
						<label class="tcwb-print-field">
							<span>Width (in) <span class="tcwb-required">*</span></span>
							<input type="number" required min="0.01" step="0.01" class="form-control input-xs" data-field="width" value="${this._num(sec.width) || ''}">
						</label>
						${laser_field}
						<label class="tcwb-print-field">
							<span>No. of Thread Colour</span>
							<input type="number" min="0" step="1" class="form-control input-xs" data-field="no_of_thread_colours" value="${this._num(sec.no_of_thread_colours) || ''}">
						</label>
					</div>
					<div class="tcwb-print-breakdown">
						<div class="tcwb-print-facts">
							${this._render_emb_facts(computed)}
						</div>
						<div class="tcwb-print-calc-wrap">
							${this._render_emb_calc(computed)}
						</div>
					</div>
				</div>
			</div>
		`;
	}

	_render_emb_facts(computed) {
		const esc = frappe.utils.escape_html;
		const facts = [
			['Bobbin Thread Avg.', this._num(computed.bobbin).toFixed(2), 'bobbin'],
			['Cost / Mtr',         this._fmt_currency(computed.cost_per_mtr), 'cost_per_mtr'],
			['Thread Cost',        this._fmt_currency(computed.thread_cost), 'thread_cost'],
			['Stitch Rate',        this._fmt_currency(computed.stitch_rate), 'stitch_rate'],
			['Cost / 1000ST',      this._fmt_currency(computed.cost_per_1000st), 'cost_per_1000st'],
			['Area',               `${computed.area.toFixed(2)} sq in`, 'area'],
			['Canvas Cost',        this._fmt_currency(computed.canvas_cost), 'canvas_cost'],
			['Laser Cost',         this._fmt_currency(computed.laser_cost), 'laser_cost'],
			['Manpower',           this._fmt_currency(computed.manpower_cost), 'manpower'],
			['Over Head Amt',      this._fmt_currency(computed.overhead_amount), 'overhead_amount'],
			['Rejection Amt',      this._fmt_currency(computed.rejection_amount), 'rejection_amount'],
		];
		const rows = facts.map(([label, value, key]) => `
			<div class="tcwb-fact">
				<dt>${esc(label)}</dt>
				<dd><span data-emb-value="${esc(key)}_fact">${value}</span></dd>
			</div>
		`).join('');
		return `<dl class="tcwb-facts">${rows}</dl>`;
	}

	_render_emb_calc(computed) {
		const calc_row = (op, label, value_key, value_text, extra_cls = '') => `
			<div class="tcwb-calc-row${extra_cls ? ' ' + extra_cls : ''}">
				<span class="tcwb-calc-op">${op}</span>
				<span class="tcwb-calc-label">${label}</span>
				<span class="tcwb-calc-amount" data-emb-value="${value_key}">${value_text}</span>
			</div>
		`;
		const oh_pct = `<span class="tcwb-calc-pct" data-emb-value="overhead_percent_label">(${this._fmt_percent(computed.overhead_percent)})</span>`;
		const rej_pct = `<span class="tcwb-calc-pct" data-emb-value="rejection_percent_label">(${this._fmt_percent(computed.rejection_percent)})</span>`;
		return `
			<div class="tcwb-calc">
				${calc_row('',  'Thread Cost',     'thread_cost',     this._fmt_currency(computed.thread_cost))}
				${calc_row('+', 'Cost / 1000ST',   'cost_per_1000st', this._fmt_currency(computed.cost_per_1000st))}
				${calc_row('+', 'Canvas Cost',     'canvas_cost',     this._fmt_currency(computed.canvas_cost))}
				${calc_row('+', 'Manpower',        'manpower',        this._fmt_currency(computed.manpower_cost))}
				${calc_row('+', 'Laser Cost',      'laser_cost',      this._fmt_currency(computed.laser_cost))}
				${calc_row('=', 'Cost',            'cost',            this._fmt_currency(computed.cost), 'subtotal')}
				${calc_row('+', `Over Head ${oh_pct}`, 'overhead_amount', this._fmt_currency(computed.overhead_amount))}
				${calc_row('+', `Rejection ${rej_pct}`, 'rejection_amount', this._fmt_currency(computed.rejection_amount), 'warning')}
				${calc_row('=', 'Final Cost',      'final_cost',      this._fmt_currency(computed.final_cost), 'total')}
			</div>
		`;
	}

	_recompute_emb_panel() {
		const ec = this._get_emb_state();
		let grand_total = 0;
		const $panel = this.$els.detail.find('[data-tab-panel="embroidery"]');

		(ec.sections || []).forEach((sec) => {
			const computed = this._compute_emb_section(sec);
			grand_total += computed.final_cost;
			const $sec = $panel.find(`.tcwb-emb-section[data-emb-id="${sec.id}"]`);
			if (!$sec.length) return;
			$sec.find('[data-role="emb-section-final"]').text(this._fmt_currency(computed.final_cost));

			const setValue = (key, value) => {
				$sec.find(`[data-emb-value="${key}"]`).text(value);
				$sec.find(`[data-emb-value="${key}_fact"]`).text(value);
			};
			setValue('bobbin', this._num(computed.bobbin).toFixed(2));
			setValue('cost_per_mtr', this._fmt_currency(computed.cost_per_mtr));
			setValue('thread_cost', this._fmt_currency(computed.thread_cost));
			setValue('stitch_rate', this._fmt_currency(computed.stitch_rate));
			setValue('cost_per_1000st', this._fmt_currency(computed.cost_per_1000st));
			setValue('area', `${computed.area.toFixed(2)} sq in`);
			setValue('canvas_cost', this._fmt_currency(computed.canvas_cost));
			setValue('laser_cost', this._fmt_currency(computed.laser_cost));
			setValue('manpower', this._fmt_currency(computed.manpower_cost));
			setValue('cost', this._fmt_currency(computed.cost));
			setValue('overhead_amount', this._fmt_currency(computed.overhead_amount));
			setValue('rejection_amount', this._fmt_currency(computed.rejection_amount));
			setValue('final_cost', this._fmt_currency(computed.final_cost));
			$sec.find('[data-emb-value="overhead_percent_label"]').text(`(${this._fmt_percent(computed.overhead_percent)})`);
			$sec.find('[data-emb-value="rejection_percent_label"]').text(`(${this._fmt_percent(computed.rejection_percent)})`);

			// Toggle laser field visibility based on EMB type
			const $laser = $sec.find('[data-applique-only]');
			if (computed.is_applique) {
				$laser.show();
			} else {
				$laser.hide();
			}
		});

		$panel.find('[data-role="emb-grand-total"]').text(this._fmt_currency(grand_total));
		ec.total_embroidery_cost = grand_total;
		this._recompute_final_rollup_panel();
	}

	_rerender_emb_panel() {
		const $panel = this.$els.detail.find('[data-tab-panel="embroidery"]');
		const open_ids = new Set();
		$panel.find('.tcwb-emb-section.is-open').each((_, el) => {
			const id = $(el).attr('data-emb-id');
			if (id) open_ids.add(id);
		});
		$panel.html(this._render_emb_panel(this.state.detail_data && this.state.detail_data.embroidery_cost, open_ids));
		this._wire_emb_panel();
	}

	_wire_emb_panel() {
		const $panel = this.$els.detail.find('[data-tab-panel="embroidery"]');
		if (!$panel.length) return;

		$panel.find('[data-role="add-emb-section"]').off('click.tcwb-emb').on('click.tcwb-emb', () => {
			const ec = this._get_emb_state();
			const new_id = this._new_emb_section_id();
			ec.sections.push({
				id: new_id,
				section_name: '',
				thread_type: '',
				emb_type: '',
				no_of_stitches: 0,
				no_of_thread_colours: 0,
				needle_thread_avg: 0,
				letter_design_for_laser: 0,
				length: 0,
				width: 0,
			});
			this._rerender_emb_panel();
			this._set_save_button_state();

			const $new_sec = this.$els.detail.find(`.tcwb-emb-section[data-emb-id="${new_id}"]`);
			if ($new_sec.length) {
				$new_sec.addClass('is-open');
				$new_sec.find('.tcwb-print-section-toggle').attr('aria-expanded', 'true');
				$new_sec[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
				const $name_input = $new_sec.find('[data-field="section_name"]');
				if ($name_input.length) {
					setTimeout(() => $name_input.trigger('focus'), 0);
				}
			}
		});

		$panel.find('.tcwb-print-section-toggle').off('click.tcwb-emb').on('click.tcwb-emb', (e) => {
			const $btn = $(e.currentTarget);
			const $section = $btn.closest('.tcwb-emb-section');
			if (!$section.length) return;
			const open = !$section.hasClass('is-open');
			$section.toggleClass('is-open', open);
			$btn.attr('aria-expanded', open ? 'true' : 'false');
		});

		$panel.find('[data-role="remove-emb-section"]').off('click.tcwb-emb').on('click.tcwb-emb', (e) => {
			const $sec = $(e.currentTarget).closest('.tcwb-emb-section');
			const id = $sec.attr('data-emb-id');
			const ec = this._get_emb_state();
			const sec = (ec.sections || []).find((s) => s.id === id);
			const idx = (ec.sections || []).indexOf(sec);
			const label = (sec && sec.section_name) || `Embroidery ${idx + 1}`;
			const esc = frappe.utils.escape_html;
			frappe.confirm(
				__('Remove embroidery section <b>{0}</b>?', [esc(label)]),
				() => {
					ec.sections = (ec.sections || []).filter((s) => s.id !== id);
					this._rerender_emb_panel();
					this._set_save_button_state();
				}
			);
		});

		$panel.find('.tcwb-emb-section [data-field]').off('input.tcwb-emb change.tcwb-emb').on('input.tcwb-emb change.tcwb-emb', (e) => {
			const $input = $(e.currentTarget);
			const $sec = $input.closest('.tcwb-emb-section');
			const id = $sec.attr('data-emb-id');
			const field = $input.attr('data-field');
			const ec = this._get_emb_state();
			const sec = (ec.sections || []).find((s) => s.id === id);
			if (!sec) return;

			let val = $input.val();
			if (['section_name', 'thread_type', 'emb_type'].includes(field)) {
				sec[field] = val;
				if (field === 'section_name') {
					$sec.find('.tcwb-print-section-title').text(val || `Embroidery ${(ec.sections || []).indexOf(sec) + 1}`);
				}
			} else {
				sec[field] = this._num(val);
			}
			$input.removeClass('has-error');
			this._recompute_emb_panel();
			this._set_save_button_state();
		});
	}

	// ---------- SAM ----------
	_get_sam_costing_rules() {
		const data = this.state.detail_data || {};
		const r = data.sam_costing_rules || {};
		const fc = this._num(r.factory_cost_per_minute);
		return { factory_cost_per_minute: fc > 0 ? fc : 13 };
	}

	_compute_sam_cost(sc) {
		const rules = this._get_sam_costing_rules();
		const sam_minutes = this._num((sc || {}).sam_minutes);
		const factory_cost = rules.factory_cost_per_minute;
		const sam_cost = sam_minutes * factory_cost;
		return { sam_minutes, factory_cost, sam_cost };
	}

	_sam_input_snapshot(sc) {
		if (!sc) return JSON.stringify({ m: 0 });
		return JSON.stringify({ m: this._num(sc.sam_minutes) });
	}

	_has_sam_changes() {
		const current = this._sam_input_snapshot(this.state.detail_data && this.state.detail_data.sam_cost);
		return current !== (this.state.original_sam_snapshot || JSON.stringify({ m: 0 }));
	}

	_get_sam_state() {
		const data = this.state.detail_data || {};
		if (!data.sam_cost) {
			data.sam_cost = { sam_minutes: 0 };
			this.state.detail_data = data;
		}
		return data.sam_cost;
	}

	_validate_sam_cost() {
		const sc = (this.state.detail_data || {}).sam_cost || {};
		const errors = [];
		const invalid_fields = [];
		if (this._num(sc.sam_minutes) <= 0) {
			errors.push('SAM (minutes) must be greater than 0.');
			invalid_fields.push({ field: 'sam_minutes' });
		}
		return { valid: errors.length === 0, errors, invalid_fields };
	}

	_apply_sam_validation_ui(invalid_fields) {
		const $panel = this.$els.detail.find('[data-tab-panel="sam"]');
		$panel.find('.has-error').removeClass('has-error');
		(invalid_fields || []).forEach(({ field }) => {
			$panel.find(`[data-field="${field}"]`).addClass('has-error');
		});
	}

	_render_sam_panel(sam_cost) {
		const sc = sam_cost || { sam_minutes: 0 };
		const computed = this._compute_sam_cost(sc);
		return `
			<div class="tcwb-sam-wrap">
				<div class="tcwb-emb-header">
					<div class="tcwb-emb-header-left">
						<div class="tcwb-print-rules" title="Configured in 'SAM Costing Rules'">
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Factory Cost / min</span>
								<span class="tcwb-print-rule-value">${this._fmt_currency(computed.factory_cost)}</span>
							</div>
						</div>
					</div>
					<div class="tcwb-print-header-right">
						<div class="tcwb-print-grand">
							<span class="tcwb-print-grand-label">SAM Cost / Garment</span>
							<span class="tcwb-print-grand-value" data-role="sam-cost-total">${this._fmt_currency(computed.sam_cost)}</span>
						</div>
					</div>
				</div>
				<div class="tcwb-sam-card">
					<div class="tcwb-print-grid tcwb-sam-grid-inputs">
						<label class="tcwb-print-field">
							<span>SAM (minutes) <span class="tcwb-required">*</span></span>
							<input type="number" required min="0.01" step="0.01" class="form-control input-xs" data-field="sam_minutes" value="${this._num(sc.sam_minutes) || ''}">
						</label>
					</div>
				</div>
			</div>
		`;
	}

	_recompute_sam_panel() {
		const sc = this._get_sam_state();
		const computed = this._compute_sam_cost(sc);
		const $panel = this.$els.detail.find('[data-tab-panel="sam"]');
		$panel.find('[data-role="sam-cost-total"]').text(this._fmt_currency(computed.sam_cost));
	}

	_wire_sam_panel() {
		const $panel = this.$els.detail.find('[data-tab-panel="sam"]');
		if (!$panel.length) return;

		$panel.find('[data-field="sam_minutes"]').off('input.tcwb-sam change.tcwb-sam').on('input.tcwb-sam change.tcwb-sam', (e) => {
			const $input = $(e.currentTarget);
			const sc = this._get_sam_state();
			sc.sam_minutes = this._num($input.val());
			$input.removeClass('has-error');
			this._recompute_sam_panel();
			this._recompute_final_rollup_panel();
			this._set_save_button_state();
		});
	}

	// ---------- Final Roll-Up ----------
	_compute_base_costs() {
		const data = this.state.detail_data || {};
		const fabric_cost = this._grand_cost_per_piece(data.costing);
		const trim_cost = this._num((data.trim_costing || {}).total_trim_cost);
		const print_cost = this._num((data.print_costing || {}).total_print_cost);
		const embroidery_cost = this._num((data.embroidery_cost || {}).total_embroidery_cost);
		const sam_total = this._compute_sam_cost((data.sam_cost || {})).sam_cost;
		const base_cost = fabric_cost + trim_cost + print_cost + embroidery_cost + sam_total;
		return { fabric_cost, trim_cost, print_cost, embroidery_cost, sam_cost: sam_total, base_cost };
	}

	_compute_final_rollup() {
		const components = this._compute_base_costs();
		const fr = (this.state.detail_data || {}).final_rollup || {};
		const rejection_percent = this._num(fr.rejection_percent);
		const testing_percent = this._num(fr.testing_percent);
		const profit_percent = this._num(fr.profit_percent);
		const rejection_amount = components.base_cost * rejection_percent / 100;
		const testing_amount = components.base_cost * testing_percent / 100;
		const profit_amount = components.base_cost * profit_percent / 100;
		const final_cost = components.base_cost + rejection_amount + testing_amount + profit_amount;
		const currency_code = fr.currency_code || 'INR';
		const conversion_factor = this._num(fr.conversion_factor) > 0 ? this._num(fr.conversion_factor) : 1;
		const final_cost_in_currency = conversion_factor > 0 ? final_cost / conversion_factor : final_cost;
		return {
			...components,
			rejection_percent, testing_percent, profit_percent,
			rejection_amount, testing_amount, profit_amount,
			final_cost,
			currency_code, conversion_factor, final_cost_in_currency,
		};
	}

	_get_costing_currency_options() {
		const data = this.state.detail_data || {};
		if (Array.isArray(data.costing_currency_options) && data.costing_currency_options.length) {
			return data.costing_currency_options;
		}
		return [{ currency_code: 'INR', description: 'Indian Rupee', conversion_factor: 1, currency_symbol: '₹' }];
	}

	_currency_meta(code) {
		const opts = this._get_costing_currency_options();
		return opts.find((o) => (o.currency_code || '').toUpperCase() === (code || 'INR').toUpperCase()) || opts[0];
	}

	_fmt_currency_amount(value, symbol) {
		if (value === null || value === undefined || value === '') return '—';
		const n = Number(value);
		if (!isFinite(n)) return '—';
		const sym = symbol || '₹';
		return `${sym}${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
	}

	_final_rollup_input_snapshot(fr) {
		const empty = { r: 0, t: 0, p: 0, c: 'INR', cf: 1 };
		if (!fr) return JSON.stringify(empty);
		return JSON.stringify({
			r: this._num(fr.rejection_percent),
			t: this._num(fr.testing_percent),
			p: this._num(fr.profit_percent),
			c: fr.currency_code || 'INR',
			cf: this._num(fr.conversion_factor) || 1,
		});
	}

	_has_final_rollup_changes() {
		const current = this._final_rollup_input_snapshot(this.state.detail_data && this.state.detail_data.final_rollup);
		return current !== (this.state.original_final_rollup_snapshot || JSON.stringify({ r: 0, t: 0, p: 0, c: 'INR', cf: 1 }));
	}

	_get_final_rollup_state() {
		const data = this.state.detail_data || {};
		if (!data.final_rollup) {
			data.final_rollup = { rejection_percent: 0, testing_percent: 0, profit_percent: 0 };
			this.state.detail_data = data;
		}
		return data.final_rollup;
	}

	_validate_final_rollup() {
		const fr = (this.state.detail_data || {}).final_rollup || {};
		const errors = [];
		const invalid_fields = [];
		[
			['rejection_percent', 'Rejection %'],
			['testing_percent', 'Testing %'],
			['profit_percent', 'Profit %'],
		].forEach(([f, fname]) => {
			if (this._num(fr[f]) < 0) {
				errors.push(`${fname} must be 0 or greater.`);
				invalid_fields.push({ field: f });
			}
		});
		const code = (fr.currency_code || 'INR').toUpperCase();
		if (code !== 'INR' && this._num(fr.conversion_factor) <= 0) {
			errors.push('Conversion Factor must be greater than 0.');
			invalid_fields.push({ field: 'conversion_factor' });
		}
		return { valid: errors.length === 0, errors, invalid_fields };
	}

	_apply_final_rollup_validation_ui(invalid_fields) {
		const $panel = this.$els.detail.find('[data-tab-panel="rollup"]');
		$panel.find('.has-error').removeClass('has-error');
		(invalid_fields || []).forEach(({ field }) => {
			$panel.find(`[data-field="${field}"]`).addClass('has-error');
		});
	}

	_render_final_rollup_panel(final_rollup) {
		const esc = frappe.utils.escape_html;
		const fr = final_rollup || { rejection_percent: 0, testing_percent: 0, profit_percent: 0, currency_code: 'INR', conversion_factor: 1 };
		const c = this._compute_final_rollup();
		const data_root = this.state.detail_data || {};
		const garment_items_html = (() => {
			const items = [
				['Style:', data_root.garment_style],
				['Gender:', data_root.garment_gender],
				['Brand:', data_root.garment_brand],
			];
			return items.map(([label, value]) => {
				const display = this._is_blankish(value) ? '<span class="tcwb-fr-garment-info-empty">—</span>' : esc(String(value));
				return `<div class="tcwb-fr-garment-info-item"><span class="tcwb-fr-garment-info-label">${label}</span><span class="tcwb-fr-garment-info-value">${display}</span></div>`;
			}).join('');
		})();
		const mfr = data_root.market_fob_and_retail || {};
		const fmt_usd = (v) => `$${this._num(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
		const UNAVAILABLE = '<span class="tcwb-print-rule-unavailable">&lt;unavailable&gt;</span>';
		const market_fob_value = (mfr.market_fob != null && this._num(mfr.market_fob) > 0)
			? fmt_usd(mfr.market_fob)
			: UNAVAILABLE;
		const retail_value = (mfr.retail != null && this._num(mfr.retail) > 0)
			? fmt_usd(mfr.retail)
			: UNAVAILABLE;
		const market_fob_chip = `<div class="tcwb-print-rule"><span class="tcwb-print-rule-label">Market FOB (USD)</span><span class="tcwb-print-rule-value">${market_fob_value}</span></div>`;
		const retail_chip = `<div class="tcwb-print-rule"><span class="tcwb-print-rule-label">Retail (USD)</span><span class="tcwb-print-rule-value">${retail_value}</span></div>`;
		const sel_meta = this._currency_meta(c.currency_code);
		const sel_symbol = sel_meta.currency_symbol || sel_meta.currency_code;
		const is_inr = (c.currency_code || 'INR').toUpperCase() === 'INR';
		const currency_options = this._get_costing_currency_options().map((o) => {
			const selected = (o.currency_code || '').toUpperCase() === (c.currency_code || 'INR').toUpperCase() ? ' selected' : '';
			const label = o.currency_code + (o.description ? ` — ${o.description}` : '');
			return `<option value="${esc(o.currency_code)}" data-conversion-factor="${this._num(o.conversion_factor) || 1}" data-symbol="${esc(o.currency_symbol || '')}"${selected}>${esc(label)}</option>`;
		}).join('');
		const calc_row = (op, label, value_key, value_text, extra_cls = '') => `
			<div class="tcwb-calc-row${extra_cls ? ' ' + extra_cls : ''}">
				<span class="tcwb-calc-op">${op}</span>
				<span class="tcwb-calc-label">${label}</span>
				<span class="tcwb-calc-amount" data-fr-value="${value_key}">${value_text}</span>
			</div>
		`;
		const pct_input = (field, value) => `
			<span class="tcwb-fr-pct-input">
				<input type="number" min="0" step="0.01" class="form-control input-xs" data-field="${field}" value="${this._num(value) || ''}">
				<span class="tcwb-fr-pct-suffix">%</span>
			</span>
		`;

		return `
			<div class="tcwb-fr-wrap">
				<div class="tcwb-emb-header">
					<div class="tcwb-emb-header-left">
						<div class="tcwb-print-rules">
							<div class="tcwb-print-rule">
								<span class="tcwb-print-rule-label">Base Cost / Garment</span>
								<span class="tcwb-print-rule-value" data-fr-value="base_cost_chip">${this._fmt_currency(c.base_cost)}</span>
							</div>
							${market_fob_chip}
							${retail_chip}
						</div>
					</div>
					<div class="tcwb-print-header-right">
						<div class="tcwb-print-grand">
							<span class="tcwb-print-grand-label">Final Cost / Garment</span>
							<span class="tcwb-print-grand-value" data-fr-value="final_cost_grand">${this._fmt_currency_amount(c.final_cost_in_currency, sel_symbol)}</span>
						</div>
					</div>
				</div>

				<div class="tcwb-fr-garment-info">${garment_items_html}</div>

				<div class="tcwb-fr-card">
					<div class="tcwb-calc">
						${calc_row('',  'Fabric (per piece)', 'fabric_cost',     this._fmt_currency(c.fabric_cost))}
						${calc_row('+', 'Trim',               'trim_cost',       this._fmt_currency(c.trim_cost))}
						${calc_row('+', 'Print',              'print_cost',      this._fmt_currency(c.print_cost))}
						${calc_row('+', 'Embroidery',         'embroidery_cost', this._fmt_currency(c.embroidery_cost))}
						${calc_row('+', 'SAM',                'sam_cost',        this._fmt_currency(c.sam_cost))}
						${calc_row('=', 'Base Cost',          'base_cost',       this._fmt_currency(c.base_cost), 'subtotal')}
						${calc_row('+', `<span class="tcwb-fr-pct-label">Rejection</span>${pct_input('rejection_percent', fr.rejection_percent)}`, 'rejection_amount', this._fmt_currency(c.rejection_amount))}
						${calc_row('+', `<span class="tcwb-fr-pct-label">Testing</span>${pct_input('testing_percent', fr.testing_percent)}`,       'testing_amount',   this._fmt_currency(c.testing_amount))}
						${calc_row('+', `<span class="tcwb-fr-pct-label">Profit</span>${pct_input('profit_percent', fr.profit_percent)}`,         'profit_amount',    this._fmt_currency(c.profit_amount))}
						${calc_row('=', 'Final Cost / Garment (INR)', 'final_cost', this._fmt_currency(c.final_cost), 'total')}
					</div>
					<div class="tcwb-fr-currency-row">
						<div class="tcwb-fr-currency-field">
							<span class="tcwb-fr-currency-label">Currency</span>
							<select class="form-control input-xs tcwb-fr-currency-select" data-field="currency_code">${currency_options}</select>
						</div>
						<div class="tcwb-fr-currency-field tcwb-fr-conv-factor" style="${is_inr ? 'display:none;' : ''}">
							<span class="tcwb-fr-currency-label">Conversion Factor</span>
							<input type="number" min="0.01" step="0.01" class="form-control input-xs" data-field="conversion_factor" value="${this._num(c.conversion_factor)}">
						</div>
						<div class="tcwb-fr-currency-field tcwb-fr-final-converted" style="${is_inr ? 'display:none;' : ''}">
							<span class="tcwb-fr-currency-label">Final Cost (<span data-fr-value="currency_code_label">${esc(c.currency_code)}</span>)</span>
							<span class="tcwb-fr-final-converted-value" data-fr-value="final_cost_converted">${this._fmt_currency_amount(c.final_cost_in_currency, sel_symbol)}</span>
						</div>
					</div>
				</div>
			</div>
		`;
	}

	_recompute_final_rollup_panel() {
		const $panel = this.$els.detail.find('[data-tab-panel="rollup"]');
		if (!$panel.length) return;
		const c = this._compute_final_rollup();
		const sel_meta = this._currency_meta(c.currency_code);
		const sel_symbol = sel_meta.currency_symbol || sel_meta.currency_code;
		const is_inr = (c.currency_code || 'INR').toUpperCase() === 'INR';
		const setValue = (key, value) => $panel.find(`[data-fr-value="${key}"]`).text(value);
		setValue('fabric_cost', this._fmt_currency(c.fabric_cost));
		setValue('trim_cost', this._fmt_currency(c.trim_cost));
		setValue('print_cost', this._fmt_currency(c.print_cost));
		setValue('embroidery_cost', this._fmt_currency(c.embroidery_cost));
		setValue('sam_cost', this._fmt_currency(c.sam_cost));
		setValue('base_cost', this._fmt_currency(c.base_cost));
		setValue('base_cost_chip', this._fmt_currency(c.base_cost));
		setValue('rejection_amount', this._fmt_currency(c.rejection_amount));
		setValue('testing_amount', this._fmt_currency(c.testing_amount));
		setValue('profit_amount', this._fmt_currency(c.profit_amount));
		setValue('final_cost', this._fmt_currency(c.final_cost));
		setValue('final_cost_grand', this._fmt_currency_amount(c.final_cost_in_currency, sel_symbol));
		setValue('currency_code_label', c.currency_code);
		setValue('final_cost_converted', this._fmt_currency_amount(c.final_cost_in_currency, sel_symbol));
		$panel.find('.tcwb-fr-conv-factor').toggle(!is_inr);
		$panel.find('.tcwb-fr-final-converted').toggle(!is_inr);
	}

	_wire_final_rollup_panel() {
		const $panel = this.$els.detail.find('[data-tab-panel="rollup"]');
		if (!$panel.length) return;

		$panel.find('[data-field="currency_code"]').off('change.tcwb-fr').on('change.tcwb-fr', (e) => {
			const $select = $(e.currentTarget);
			const code = $select.val() || 'INR';
			const meta = this._currency_meta(code);
			const default_cf = this._num(meta.conversion_factor) > 0 ? this._num(meta.conversion_factor) : 1;
			const fr = this._get_final_rollup_state();
			fr.currency_code = code;
			fr.conversion_factor = default_cf;
			$panel.find('[data-field="conversion_factor"]').val(default_cf);
			this._recompute_final_rollup_panel();
			this._set_save_button_state();
		});

		$panel.find('[data-field="conversion_factor"]').off('input.tcwb-fr change.tcwb-fr').on('input.tcwb-fr change.tcwb-fr', (e) => {
			const $input = $(e.currentTarget);
			const fr = this._get_final_rollup_state();
			fr.conversion_factor = this._num($input.val());
			$input.removeClass('has-error');
			this._recompute_final_rollup_panel();
			this._set_save_button_state();
		});

		$panel.find('[data-field="rejection_percent"], [data-field="testing_percent"], [data-field="profit_percent"]').off('input.tcwb-fr change.tcwb-fr').on('input.tcwb-fr change.tcwb-fr', (e) => {
			const $input = $(e.currentTarget);
			const field = $input.attr('data-field');
			const fr = this._get_final_rollup_state();
			fr[field] = this._num($input.val());
			$input.removeClass('has-error');
			this._recompute_final_rollup_panel();
			this._set_save_button_state();
		});
	}

	// ---------- section rendering ----------
	_render_section(section, idx) {
		const esc = frappe.utils.escape_html;
		const data = (section && section.costing) || {};
		const name = (section && section.section) || `Section ${idx + 1}`;
		const open = idx === 0; // open first by default

		const cpk = data.cost_per_kg || {};
		const breakup = cpk.breakup || {};
		const base_total_cost = this._num(cpk.total_fabric_cost);
		const effective_base = this._section_effective_base_total(section);
		const adjustment_percent = this._section_adjustment(section);
		const total_cost = this._calc_adjusted_total(effective_base, adjustment_percent);
		const delta = total_cost - effective_base;

		const head_keys = [
			{ key: 'yarn', label: 'Yarn' },
			{ key: 'knitting', label: 'Knitting' },
			{ key: 'dyes_and_chemicals', label: 'Dyes & Chemicals' },
			{ key: 'mechanical_chemical_finish', label: 'Mech / Chem Finish' },
			{ key: 'finishing_charges', label: 'Finishing' },
		];

		const sum_of_heads = head_keys.reduce((acc, k) => acc + (this._num((breakup[k.key] || {}).cost_per_kg) || 0), 0);
		const gross = this._num(cpk.gross_total_cost) || sum_of_heads;
		const loss_amt = this._num(cpk.loss_amount);
		const total_fabric = this._num(cpk.total_fabric_cost);
		const loss_pct = data.loss_percent != null ? this._num(data.loss_percent) : null;

		// Adjusted heads: optional per-head overrides
		const adj_head_value = (key) => {
			const raw = (breakup[key] || {}).adjusted_cost_per_kg;
			return this._is_blankish(raw) ? null : this._num(raw);
		};
		const has_any_adjusted = head_keys.some((k) => adj_head_value(k.key) !== null);
		const adj_gross = has_any_adjusted
			? head_keys.reduce((acc, k) => {
				const adj = adj_head_value(k.key);
				return acc + (adj !== null ? adj : this._num((breakup[k.key] || {}).cost_per_kg));
			}, 0)
			: null;
		const adj_loss = (has_any_adjusted && loss_pct != null) ? adj_gross * (loss_pct / 100) : null;
		const adj_total = has_any_adjusted ? adj_gross + (adj_loss || 0) : null;

		const adj_delta_cls = (adj, sys) => {
			if (adj === null || adj === undefined) return '';
			if (Math.abs(adj - sys) < 0.0001) return '';
			return adj > sys ? ' is-pos' : ' is-neg';
		};
		const adj_subtotal_cls = (adj, sys) => {
			if (adj === null || adj === undefined) return '';
			if (Math.abs(adj - sys) < 0.0001) return ' is-equal';
			return adj > sys ? ' is-pos' : ' is-neg';
		};

		const head_rows = head_keys.map((k, i) => {
			const v = this._num((breakup[k.key] || {}).cost_per_kg);
			const label_html = esc(k.label);
			const adj = adj_head_value(k.key);
			const adj_value = adj === null ? '' : this._num(adj).toFixed(2);
			const input_cls = adj_delta_cls(adj, v);
			const remark = (breakup[k.key] || {}).remark || '';
			return `
				<div class="tcwb-calc-row">
					<span class="tcwb-calc-op">${i === 0 ? '' : '+'}</span>
					<span class="tcwb-calc-label">${label_html}</span>
					<span class="tcwb-calc-amount">${this._fmt_currency(v)}</span>
					<span class="tcwb-calc-adjusted">
						<input type="text" inputmode="decimal" class="form-control input-xs tcwb-calc-adj-input${input_cls}" data-section-idx="${idx}" data-head-key="${esc(k.key)}" value="${adj_value}">
					</span>
					<span class="tcwb-calc-remark">
						<input type="text" class="form-control input-xs tcwb-calc-remark-input" data-section-idx="${idx}" data-head-key="${esc(k.key)}" value="${esc(remark)}" placeholder="Remark…">
					</span>
				</div>
			`;
		}).join('');

		const fmt_adj = (v) => (v === null || v === undefined) ? '' : this._fmt_currency(v);
		const calc_html = `
			<div class="tcwb-calc tcwb-fabric-calc-split" data-section-idx="${idx}">
				<div class="tcwb-calc-row tcwb-calc-header">
					<span class="tcwb-calc-op"></span>
					<span class="tcwb-calc-label"></span>
					<span class="tcwb-calc-col-header">System</span>
					<span class="tcwb-calc-col-header">Adjusted</span>
					<span class="tcwb-calc-col-header">Remarks</span>
				</div>
				${head_rows}
				<div class="tcwb-calc-row subtotal">
					<span class="tcwb-calc-op">=</span>
					<span class="tcwb-calc-label">Gross / kg</span>
					<span class="tcwb-calc-amount">${this._fmt_currency(gross)}</span>
					<span class="tcwb-calc-adjusted${adj_subtotal_cls(adj_gross, gross)}" data-role="fabric-adj-gross">${fmt_adj(adj_gross)}</span>
					<span class="tcwb-calc-remark"></span>
				</div>
				<div class="tcwb-calc-row warning">
					<span class="tcwb-calc-op">+</span>
					<span class="tcwb-calc-label">Loss${loss_pct != null ? ` <span class="tcwb-calc-pct">(${this._fmt_percent(loss_pct)})</span>` : ''}</span>
					<span class="tcwb-calc-amount">${this._fmt_currency(loss_amt)}</span>
					<span class="tcwb-calc-adjusted" data-role="fabric-adj-loss">${fmt_adj(adj_loss)}</span>
					<span class="tcwb-calc-remark"></span>
				</div>
				<div class="tcwb-calc-row total${has_any_adjusted ? ' has-adjusted' : ''}">
					<span class="tcwb-calc-op">=</span>
					<span class="tcwb-calc-label">Total / kg</span>
					<span class="tcwb-calc-amount">${this._fmt_currency(total_fabric)}</span>
					<span class="tcwb-calc-adjusted${adj_subtotal_cls(adj_total, total_fabric)}" data-role="fabric-adj-total">${fmt_adj(adj_total)}</span>
					<span class="tcwb-calc-remark"></span>
				</div>
			</div>
		`;

		const fabric_header = this._render_fabric_header(data, calc_html);

		const cost_heads_html = head_keys.map((k) => {
			const v = breakup[k.key];
			if (!v) return '';
			return this._render_cost_head(k.key, k.label, v, idx, section);
		}).join('');

		return `
			<div class="tcwb-section${open ? ' is-open' : ''}" data-section-idx="${idx}">
				<div class="tcwb-section-head">
					<button class="tcwb-section-toggle" type="button" aria-expanded="${open ? 'true' : 'false'}">
						<span class="tcwb-section-caret">›</span>
						<span class="tcwb-section-name">${esc(name)}</span>
					</button>
					<div class="tcwb-section-controls">
						<div class="tcwb-metric-row">
							<span class="tcwb-metric-label">Total / kg</span>
							<span class="tcwb-metric-value tcwb-metric-value-base" data-role="section-total-per-kg">${this._fmt_currency(effective_base)}</span>
						</div>
						<div class="tcwb-metric-row tcwb-metric-row-adjustment">
							<span class="tcwb-metric-label">Adjustment</span>
							<span class="tcwb-metric-adjust-input">
								<input type="number" step="0.01" class="form-control input-xs tcwb-adjustment-input" data-role="adjustment-input" data-idx="${idx}" value="${this._fmt_adjustment_input(adjustment_percent)}">
								<span class="tcwb-adjustment-suffix">%</span>
							</span>
							<span class="tcwb-section-total-delta${delta > 0 ? ' is-pos' : (delta < 0 ? ' is-neg' : '')}" data-role="section-total-delta">${this._fmt_signed_currency(delta)}</span>
						</div>
						<div class="tcwb-metric-row">
							<span class="tcwb-metric-label">Final / kg</span>
							<span class="tcwb-metric-value tcwb-section-total-value" data-role="section-total-value">${this._fmt_currency(total_cost)}</span>
						</div>
						<div class="tcwb-metric-row tcwb-metric-row-kg">
							<span class="tcwb-metric-label">Consumption (Grams / Piece)</span>
							<span class="tcwb-metric-adjust-input">
								<input type="number" step="0.01" min="0.01" class="form-control input-xs tcwb-kg-per-piece-input" data-role="kg-per-piece-input" data-idx="${idx}" value="${this._fmt_grams_per_piece_input(this._section_kg_per_piece(section))}">
							</span>
						</div>
						<div class="tcwb-metric-row">
							<span class="tcwb-metric-label">Cost / Piece</span>
							<span class="tcwb-metric-value tcwb-section-cost-per-piece" data-role="section-cost-per-piece">${this._fmt_currency(this._section_cost_per_piece(section))}</span>
						</div>
					</div>
				</div>
				<div class="tcwb-section-body">
					${fabric_header}
					<div class="tcwb-heads-grid">${cost_heads_html}</div>
				</div>
			</div>
		`;
	}

	_render_cost_head(key, label, data, idx, section) {
		switch (key) {
			case 'yarn':
				return this._render_yarn(label, data);
			case 'knitting':
				return this._render_knitting(label, data);
			case 'dyes_and_chemicals':
				return this._render_dyes(label, data, idx, ((section || {}).costing || {}).print_type);
			case 'mechanical_chemical_finish':
				return this._render_mnc_finish(label, data);
			case 'finishing_charges':
				return this._render_finishing(label, data);
			default:
				return this._render_generic_card(label, data);
		}
	}

	_render_card_shell(label, totalLabel, totalValue, body) {
		return `
			<div class="tcwb-card"${arguments[4] ? ` data-head-key="${frappe.utils.escape_html(arguments[4])}"` : ''}>
				<div class="tcwb-card-head">
					<div class="tcwb-card-title">${frappe.utils.escape_html(label)}</div>
					<div class="tcwb-card-total">
						<span class="tcwb-card-total-label">${frappe.utils.escape_html(totalLabel)}</span>
						<span class="tcwb-card-total-value">${totalValue}</span>
					</div>
				</div>
				<div class="tcwb-card-body">${body}</div>
			</div>
		`;
	}

	_render_yarn(label, data) {
		const esc = frappe.utils.escape_html;
		const yarns = data.yarns || [];
		const rows = yarns.map((y) => {
			const has_error = !!y.ERROR;
			const error_badge = has_error
				? `<span class="tcwb-badge danger" title="${esc(y.ERROR)}">⚠ Error</span>`
				: '';
			const desc_or_err = has_error ? `<span class="tcwb-error-text">${esc(y.ERROR)}</span>` : esc(y.description || '—');
			return `
				<tr class="${has_error ? 'has-error' : ''}">
					<td class="tcwb-mono">${esc(y.code || '—')} ${error_badge}</td>
					<td>${desc_or_err}</td>
					<td class="num">${this._fmt_percent(y.percent)}</td>
					<td class="num">${this._fmt_currency(y.rate_per_kg)}</td>
					<td class="num">${this._fmt_currency(y.effective_cost_per_kg)}</td>
				</tr>
			`;
		}).join('');

		const body = `
			<table class="tcwb-table">
				<thead>
					<tr><th>Code</th><th>Description</th><th class="num">%</th><th class="num">Rate / kg</th><th class="num">Eff. / kg</th></tr>
				</thead>
				<tbody>${rows || '<tr><td colspan="5" class="tcwb-empty">No yarn entries.</td></tr>'}</tbody>
			</table>
		`;
		return this._render_card_shell(label, 'Cost / kg', this._fmt_currency(data.cost_per_kg), body);
	}

	_render_knitting(label, data) {
		const esc = frappe.utils.escape_html;
		const body = `
			<div class="tcwb-kv-row">
				<div class="tcwb-kv">
					<dt>Code</dt>
					<dd class="tcwb-mono">${this._safe_text(data.code)}</dd>
				</div>
				${data.ERROR ? `<div class="tcwb-alert">${esc(data.ERROR)}</div>` : ''}
			</div>
		`;
		return this._render_card_shell(label, 'Cost / kg', this._fmt_currency(data.cost_per_kg), body);
	}

	_render_dyes(label, data, idx, current_print_type) {
		const esc = frappe.utils.escape_html;
		const breakup = data.breakup || {};
		const known = [
			['dnc_single_pass', 'Single Pass'],
			['dnc_double_pass', 'Double Pass'],
			['aop', 'AOP'],
			['digital', 'Digital'],
		];

		const rows = [];
		known.forEach(([k, name]) => {
			if (breakup[k] != null) {
				rows.push(`<tr><td>${esc(name)}</td><td class="num">${this._fmt_currency(breakup[k])}</td></tr>`);
			}
		});
		Object.keys(breakup).forEach((k) => {
			if (!known.find((x) => x[0] === k)) {
				rows.push(`<tr><td>${esc(this._humanize(k))}</td><td class="num">${this._fmt_currency(breakup[k])}</td></tr>`);
			}
		});

		const cur_pt = (current_print_type || 'None');
		const sec_idx = idx == null ? '' : idx;
		const radio_html = `
			<div class="tcwb-dyes-print-type" data-section-idx="${sec_idx}">
				<span class="tcwb-dyes-print-type-label">Print Type:</span>
				${['None', 'AOP', 'Digital'].map((opt) => `
					<label class="tcwb-radio-inline">
						<input type="radio" name="dyes-print-type-${sec_idx}" value="${opt}"${opt === cur_pt ? ' checked' : ''} data-section-idx="${sec_idx}" data-role="dyes-print-type">
						<span>${opt}</span>
					</label>
				`).join('')}
			</div>
		`;

		const body = `
			${radio_html}
			<table class="tcwb-table compact">
				<thead><tr><th>Component</th><th class="num">Cost / kg</th></tr></thead>
				<tbody>${rows.join('') || '<tr><td colspan="2" class="tcwb-empty">No breakup available.</td></tr>'}</tbody>
			</table>
		`;
		return this._render_card_shell(label, 'Cost / kg', this._fmt_currency(data.cost_per_kg), body, 'dyes_and_chemicals');
	}

	_render_mnc_finish(label, data) {
		const esc = frappe.utils.escape_html;
		const procs = data.processes || {};
		const make_table = (list, title) => {
			const rows = (list || []).map((p) => `
				<tr>
					<td>${esc(p.process_type || '—')}</td>
					<td class="num">${this._fmt_currency(p.cost)}</td>
				</tr>
			`).join('');
			return `
				<div class="tcwb-subgroup">
					<div class="tcwb-subgroup-title">${esc(title)}</div>
					<table class="tcwb-table compact">
						<thead><tr><th>Process</th><th class="num">Cost / kg</th></tr></thead>
						<tbody>${rows || `<tr><td colspan="2" class="tcwb-empty">No ${title.toLowerCase()} processes.</td></tr>`}</tbody>
					</table>
				</div>
			`;
		};

		const body = `
			<div class="tcwb-subgroups">
				${make_table(procs.mechanical, 'Mechanical')}
				${make_table(procs.chemical, 'Chemical')}
			</div>
		`;
		return this._render_card_shell(label, 'Cost / kg', this._fmt_currency(data.cost_per_kg), body);
	}

	_render_finishing(label, data) {
		const esc = frappe.utils.escape_html;
		const blend = data.blend || [];
		const breakup = data.breakup || {};
		const known = [
			['dryer', 'Dryer'],
			['stentor_1st_pass', 'Stentor 1st Pass'],
			['stentor_2nd_pass', 'Stentor 2nd Pass'],
			['compactor', 'Compactor'],
		];

		const fmt_finishing = (v) => {
			const n = this._num(v);
			return n === 0 ? '<span class="tcwb-muted">N/A</span>' : this._fmt_currency(v);
		};

		const rows = [];
		known.forEach(([k, name]) => {
			if (breakup[k] != null) {
				rows.push(`<tr><td>${esc(name)}</td><td class="num">${fmt_finishing(breakup[k])}</td></tr>`);
			}
		});
		Object.keys(breakup).forEach((k) => {
			if (!known.find((x) => x[0] === k)) {
				rows.push(`<tr><td>${esc(this._humanize(k))}</td><td class="num">${fmt_finishing(breakup[k])}</td></tr>`);
			}
		});

		const blend_html = blend.length
			? `<div class="tcwb-blend-tags">${blend.map((b) => `<span class="tcwb-tag">${esc(b)}</span>`).join('')}</div>`
			: '';

		const body = `
			${blend_html}
			<table class="tcwb-table compact">
				<thead><tr><th>Process</th><th class="num">Cost / kg</th></tr></thead>
				<tbody>${rows.join('') || '<tr><td colspan="2" class="tcwb-empty">No breakup available.</td></tr>'}</tbody>
			</table>
		`;
		return this._render_card_shell(label, 'Cost / kg', this._fmt_currency(data.cost_per_kg), body);
	}

	_render_generic_card(label, data) {
		const body = `<pre class="tcwb-json">${frappe.utils.escape_html(JSON.stringify(data, null, 2))}</pre>`;
		return this._render_card_shell(label, '', '', body);
	}

	_render_trim_panel(trim_costing) {
		if (!trim_costing) {
			return '<div class="tcwb-detail-empty">No trim costing data is available for this record yet.</div>';
		}
		const esc = frappe.utils.escape_html;
		const trims = Array.isArray(trim_costing.trims) ? trim_costing.trims : [];
		const total_cost = this._num(trim_costing.total_trim_cost);
		const has_groupable = trims.some((t) => !this._is_blankish(t && t.trim_group));

		const rows = trims.map((t) => {
			const row = t || {};
			const has_group = !this._is_blankish(row.trim_group);
			const row_disabled = has_group && row.is_selected === false;
			const checkbox_cell = has_groupable
				? `<td class="tcwb-trim-select">${
					has_group
						? `<input type="checkbox" class="tcwb-trim-checkbox" data-trim-id="${esc(row.id || '')}"${row.is_selected ? ' checked' : ''}>`
						: ''
				}</td>`
				: '';

			const orig_unit_price = !this._is_blankish(row.original_unit_price) ? this._num(row.original_unit_price) : this._num(row.unit_price);
			const orig_units = !this._is_blankish(row.original_units) ? this._num(row.original_units) : this._num(row.units);
			const orig_cost = orig_unit_price * orig_units;
			const cur_unit_price = this._num(row.unit_price);
			const cur_units = this._num(row.units);
			const cur_cost = cur_unit_price * cur_units;
			const cost_delta_cls = this._trim_cost_delta_class(cur_cost, orig_cost);
			const up_delta_cls = this._trim_cost_delta_class(cur_unit_price, orig_unit_price);
			const un_delta_cls = this._trim_cost_delta_class(cur_units, orig_units);
			const fmt_units = (n) => this._num(n).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 4 });

			return `
				<tr>
					${checkbox_cell}
					<td>${esc(row.trim || '—')}</td>
					<td>${this._safe_text(row.trim_group)}</td>
					<td class="num tcwb-trim-edit-cell">
						<div class="tcwb-trim-cost-edit">
							<input type="text" inputmode="decimal" class="form-control input-xs tcwb-trim-unit-price-input${up_delta_cls ? ' ' + up_delta_cls : ''}" data-trim-id="${esc(row.id || '')}" data-orig-unit-price="${orig_unit_price}" data-orig-units="${orig_units}" value="${this._num(cur_unit_price).toFixed(2)}"${row_disabled ? ' disabled' : ''}>
							<span class="tcwb-trim-cost-original" title="Original unit price">orig: ${this._fmt_currency(orig_unit_price)}</span>
						</div>
					</td>
					<td class="num tcwb-trim-edit-cell">
						<div class="tcwb-trim-cost-edit">
							<input type="number" step="0.01" min="0" class="form-control input-xs tcwb-trim-units-input${un_delta_cls ? ' ' + un_delta_cls : ''}" data-trim-id="${esc(row.id || '')}" data-orig-units="${orig_units}" value="${cur_units}"${row_disabled ? ' disabled' : ''}>
							<span class="tcwb-trim-cost-original" title="Original units">orig: ${fmt_units(orig_units)}</span>
						</div>
					</td>
					<td class="num tcwb-trim-cost-cell">
						<div class="tcwb-trim-cost-display">
							<span class="tcwb-trim-cost-value${cost_delta_cls ? ' ' + cost_delta_cls : ''}" data-trim-cost-id="${esc(row.id || '')}" data-orig-cost="${orig_cost}">${this._fmt_currency(cur_cost)}</span>
							<span class="tcwb-trim-cost-original" title="Original cost">orig: ${this._fmt_currency(orig_cost)}</span>
						</div>
					</td>
				</tr>
			`;
		}).join('');

		const header_checkbox = has_groupable ? '<th class="tcwb-trim-select"></th>' : '';
		const table_html = trims.length
			? `
				<table class="tcwb-table tcwb-trim-table">
					<thead>
						<tr>
							${header_checkbox}
							<th>Trim</th>
							<th>Trim Group</th>
							<th class="num">Unit Price</th>
							<th class="num">Units</th>
							<th class="num">Cost</th>
						</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			`
			: '<div class="tcwb-empty">No trims listed.</div>';

		return `
			<div class="tcwb-trim-wrap">
				<div class="tcwb-trim-summary">
					<div class="tcwb-trim-summary-item">
						<span class="tcwb-trim-summary-label">Style</span>
						<span class="tcwb-trim-summary-style">${this._safe_text(trim_costing.style)}</span>
					</div>
					<div class="tcwb-trim-summary-item">
						<span class="tcwb-trim-summary-label">Total Trim Cost</span>
						<span class="tcwb-trim-summary-value">${this._fmt_currency(total_cost)}</span>
					</div>
				</div>
				${table_html}
			</div>
		`;
	}

	_render_fabric_header(data, calc_html = '') {
		const esc = frappe.utils.escape_html;
		const grey = data.grey_fabric || {};
		const gsm_mismatch = this._is_gsm_mismatch(data.gsm, data.gsm_needed);
			const items = [
				['Fabric Code', data.fabric_code, 'wide'],
				['Construction', data.construction, 'strong'],
				['Blend', data.blend, 'strong'],
				['GSM', data.gsm, 'strong'],
				['Shade', data.shade_category],
				['Grey Fabric', grey.code || grey.id],
				['Finish', data.mechanical_chemical_finish],
				['Width', data.fabric_width],
				['Loss', !this._is_blankish(data.loss_percent) ? data.loss_percent + '%' : null],
			];

		const dl = items.map(([label, value, mod]) => {
			let rendered = this._safe_text(value);
			if (label === 'GSM' && gsm_mismatch) {
				const gsm = value == null || value === '' ? '—' : esc(String(value));
				const gsm_needed = data.gsm_needed == null || data.gsm_needed === '' ? '—' : esc(String(data.gsm_needed));
				rendered = `<span class="tcwb-gsm-mismatch">${gsm}</span> (${gsm_needed})`;
			}
			return `
				<div class="tcwb-fact${mod ? ' tcwb-fact--' + mod : ''}">
					<dt>${esc(label)}</dt>
					<dd>${rendered}</dd>
				</div>
			`;
		}).join('');

		const desc = data.fabric_description ? `<div class="tcwb-fabric-desc">${esc(data.fabric_description)}</div>` : '';

		const img_html = data.image_url ? `
			<div class="tcwb-fabric-image">
				<img class="tcwb-img" src="${esc(data.image_url)}" alt="${esc(data.fabric_code || '')}" loading="lazy" />
			</div>
		` : '';

		return `
			<div class="tcwb-fabric-header">
				<div class="tcwb-fabric-total">
					${desc || '<div class="tcwb-fabric-desc tcwb-muted">No description</div>'}
				</div>
				<div class="tcwb-fabric-split">
					<div class="tcwb-fabric-attrs">
						${img_html}
						<dl class="tcwb-facts">${dl}</dl>
					</div>
					<div class="tcwb-fabric-calc">${calc_html}</div>
				</div>
			</div>
		`;
	}

	_get_costing_sections() {
		const data = this.state.detail_data || {};
		return data.costing || [];
	}

	_section_base_total(section) {
		// Backwards-compat: returns the system (pre-adjustment) total fabric cost.
		return this._num((((section || {}).costing || {}).cost_per_kg || {}).total_fabric_cost);
	}

	_section_head_keys() {
		return ['yarn', 'knitting', 'dyes_and_chemicals', 'mechanical_chemical_finish', 'finishing_charges'];
	}

	_section_effective_base_total(section) {
		// Returns the effective per-kg total, applying any per-head adjusted_cost_per_kg
		// overrides. Falls back to the system total_fabric_cost when none are present.
		const data = (section || {}).costing || {};
		const cpk = data.cost_per_kg || {};
		const breakup = cpk.breakup || {};
		const heads = this._section_head_keys();
		let has_any = false;
		let adj_gross = 0;
		heads.forEach((key) => {
			const raw = (breakup[key] || {}).adjusted_cost_per_kg;
			if (!this._is_blankish(raw)) {
				adj_gross += this._num(raw);
				has_any = true;
			} else {
				adj_gross += this._num((breakup[key] || {}).cost_per_kg);
			}
		});
		if (!has_any) return this._num(cpk.total_fabric_cost);
		const loss_pct = !this._is_blankish(data.loss_percent) ? this._num(data.loss_percent) : 0;
		return adj_gross * (1 + loss_pct / 100);
	}

	_snapshot_section_adjusted_heads(section) {
		const breakup = ((((section || {}).costing) || {}).cost_per_kg || {}).breakup || {};
		const heads = this._section_head_keys();
		const out = {};
		heads.forEach((key) => {
			const raw = (breakup[key] || {}).adjusted_cost_per_kg;
			out[key] = this._is_blankish(raw) ? null : this._num(raw);
		});
		return out;
	}

	_snapshot_section_head_remarks(section) {
		const breakup = ((((section || {}).costing) || {}).cost_per_kg || {}).breakup || {};
		const heads = this._section_head_keys();
		const out = {};
		heads.forEach((key) => {
			const raw = (breakup[key] || {}).remark;
			out[key] = raw ? String(raw) : '';
		});
		return out;
	}

	_on_dyes_print_type_change(idx, print_type) {
		const name = this.state.selected;
		if (!name) return;

		// Optimistic state update for dirty tracking
		const sections = this._get_costing_sections();
		if (sections[idx]) {
			sections[idx].costing = sections[idx].costing || {};
			sections[idx].costing.print_type = print_type;
		}
		this._set_save_button_state();

		frappe.call({
			method: 'prism.api.techpack_costing.recalculate_section_print_type',
			args: { name, idx, print_type },
		}).then((r) => {
			const res = (r && r.message) || {};
			if (!res.status) {
				frappe.msgprint({ title: __('Error'), message: __(res.error || 'Failed to recalculate.'), indicator: 'red' });
				return;
			}
			const new_section = res.data && res.data.section;
			if (!new_section) return;

			// Merge: take server's updated cost_per_kg + print_type, keep client-side adjusted_total / kg_per_piece / cost_per_piece (will recompute below)
			const cur = this._get_costing_sections()[idx] || { costing: {} };
			const cur_data = cur.costing || {};
			const new_data = new_section.costing || {};
			cur_data.cost_per_kg = new_data.cost_per_kg;
			cur_data.print_type = new_data.print_type;
			const effective_base = this._section_effective_base_total(cur);
			const adjustment = this._section_adjustment(cur);
			cur_data.adjusted_total_fabric_cost = this._calc_adjusted_total(effective_base, adjustment);
			cur_data.cost_per_piece = this._calc_cost_per_piece(cur_data.adjusted_total_fabric_cost, this._section_kg_per_piece(cur));
			cur.costing = cur_data;
			this._get_costing_sections()[idx] = cur;

			// Re-render the affected section to show updated dyes breakup + calc panel
			const $old = this.$els.detail.find(`.tcwb-section[data-section-idx="${idx}"]`);
			const was_open = $old.hasClass('is-open');
			const new_html = this._render_section(cur, idx);
			$old.replaceWith(new_html);
			if (was_open) {
				const $new = this.$els.detail.find(`.tcwb-section[data-section-idx="${idx}"]`);
				$new.addClass('is-open');
				$new.find('.tcwb-section-toggle').attr('aria-expanded', 'true');
			}

			this._refresh_section_header_metrics();
			this._set_save_button_state();
		});
	}

	_set_section_head_remark(idx, head_key, raw_value) {
		const sections = this._get_costing_sections();
		if (idx < 0 || idx >= sections.length) return;
		const row = sections[idx] || {};
		const data = row.costing || {};
		const cpk = data.cost_per_kg || {};
		const breakup = cpk.breakup || {};
		const head = breakup[head_key] || {};
		const str = raw_value == null ? '' : String(raw_value);
		if (str.trim() === '') {
			delete head.remark;
		} else {
			head.remark = str;
		}
		breakup[head_key] = head;
		cpk.breakup = breakup;
		data.cost_per_kg = cpk;
		row.costing = data;
		sections[idx] = row;
	}

	_set_section_head_adjusted(idx, head_key, raw_value) {
		const sections = this._get_costing_sections();
		if (idx < 0 || idx >= sections.length) return;
		const row = sections[idx] || {};
		const data = row.costing || {};
		const cpk = data.cost_per_kg || {};
		const breakup = cpk.breakup || {};
		const head = breakup[head_key] || {};
		const str = raw_value == null ? '' : String(raw_value).trim();
		if (str === '') {
			delete head.adjusted_cost_per_kg;
		} else {
			head.adjusted_cost_per_kg = this._num(str);
		}
		breakup[head_key] = head;
		cpk.breakup = breakup;
		data.cost_per_kg = cpk;
		// Recompute adjusted_total_fabric_cost and cost_per_piece using effective base
		const effective_base = this._section_effective_base_total(row);
		const adjustment = this._section_adjustment(row);
		data.adjusted_total_fabric_cost = this._calc_adjusted_total(effective_base, adjustment);
		data.cost_per_piece = this._calc_cost_per_piece(data.adjusted_total_fabric_cost, this._section_kg_per_piece(row));
		row.costing = data;
		sections[idx] = row;
	}

	_section_adjustment(section) {
		return this._normalize_adjustment((((section || {}).costing || {}).adjustment_percent));
	}

	_section_total_for_display(section) {
		const base = this._section_effective_base_total(section);
		const pct = this._section_adjustment(section);
		return this._calc_adjusted_total(base, pct);
	}

	_calc_adjusted_total(base_total, adjustment_percent) {
		const base = this._num(base_total);
		const pct = this._normalize_adjustment(adjustment_percent);
		return base * (1 + pct / 100);
	}

	_normalize_adjustment(value) {
		if (value === null || value === undefined || value === '') return 0;
		const n = Number(value);
		if (!isFinite(n)) return 0;
		return Math.round(n * 100) / 100;
	}

	_set_section_adjustment(idx, next_value) {
		const sections = this._get_costing_sections();
		if (idx < 0 || idx >= sections.length) return;
		const row = sections[idx] || {};
		const data = row.costing || {};
		const normalized = this._normalize_adjustment(next_value);
		data.adjustment_percent = normalized;
		const base_total = this._section_effective_base_total(row);
		data.adjusted_total_fabric_cost = this._calc_adjusted_total(base_total, normalized);
		data.cost_per_piece = this._calc_cost_per_piece(data.adjusted_total_fabric_cost, this._section_kg_per_piece(row));
		row.costing = data;
		sections[idx] = row;
	}

	_set_section_kg_per_piece(idx, next_value) {
		const sections = this._get_costing_sections();
		if (idx < 0 || idx >= sections.length) return;
		const row = sections[idx] || {};
		const data = row.costing || {};
		const normalized = this._normalize_kg_per_piece(next_value);
		data.kg_per_piece = normalized;
		data.cost_per_piece = this._calc_cost_per_piece(data.adjusted_total_fabric_cost, normalized);
		row.costing = data;
		sections[idx] = row;
	}

	_section_kg_per_piece(section) {
		return this._normalize_kg_per_piece((((section || {}).costing || {}).kg_per_piece));
	}

	_section_cost_per_piece(section) {
		const adjusted = this._num((((section || {}).costing || {}).adjusted_total_fabric_cost));
		const kg = this._section_kg_per_piece(section);
		return this._calc_cost_per_piece(adjusted, kg);
	}

	_grand_cost_per_piece(costing) {
		const sections = Array.isArray(costing) ? costing : this._get_costing_sections();
		let total = 0;
		for (const s of sections) total += this._section_cost_per_piece(s);
		return total;
	}

	_normalize_kg_per_piece(value) {
		// Stored unit is kg. Default = 0.1 kg = 100 grams (UI default).
		if (value === null || value === undefined || value === '') return 0.1;
		const n = Number(value);
		if (!isFinite(n) || n <= 0) return 0.1;
		return Math.round(n * 100000) / 100000;
	}

	_calc_cost_per_piece(adjusted_total, kg_per_piece) {
		return this._num(adjusted_total) * this._normalize_kg_per_piece(kg_per_piece);
	}

	_section_grams_per_piece(section) {
		return this._section_kg_per_piece(section) * 1000;
	}

	_fmt_grams_per_piece_input(kg_value) {
		const grams = this._num(kg_value) * 1000;
		return Number.isInteger(grams) ? String(grams) : grams.toFixed(2);
	}

	_has_adjustment_changes() {
		const sections = this._get_costing_sections();
		const originals = this.state.original_adjustments || [];
		const orig_kg = this.state.original_kg_per_piece || [];
		const orig_heads = this.state.original_adjusted_heads || [];
		const orig_remarks = this.state.original_head_remarks || [];
		if (sections.length !== originals.length) return true;

		for (let i = 0; i < sections.length; i += 1) {
			const current = this._section_adjustment(sections[i]);
			const baseline = this._normalize_adjustment(originals[i]);
			if (Math.abs(current - baseline) > 0.0001) return true;
			const current_kg = this._section_kg_per_piece(sections[i]);
			const baseline_kg = this._normalize_kg_per_piece(orig_kg[i]);
			if (Math.abs(current_kg - baseline_kg) > 0.0001) return true;
			const cur_heads = this._snapshot_section_adjusted_heads(sections[i]);
			const base_heads = orig_heads[i] || {};
			for (const k of this._section_head_keys()) {
				const c = cur_heads[k];
				const b = base_heads[k];
				if (c === null && b === null) continue;
				if (c === null || b === null) return true;
				if (Math.abs(this._num(c) - this._num(b)) > 0.0001) return true;
			}
			const cur_remarks = this._snapshot_section_head_remarks(sections[i]);
			const base_remarks = orig_remarks[i] || {};
			for (const k of this._section_head_keys()) {
				if ((cur_remarks[k] || '') !== (base_remarks[k] || '')) return true;
			}
			const cur_pt = ((sections[i].costing || {}).print_type || 'None');
			const base_pt = (this.state.original_print_types || [])[i] || 'None';
			if (cur_pt !== base_pt) return true;
		}
		return false;
	}

	_set_save_button_state() {
		const $btn = this.$els.detail.find('[data-role="save-adjustments"]');
		if (!$btn.length) return;

		const has_changes = this._has_adjustment_changes() || this._has_trim_cost_changes() || this._has_print_changes() || this._has_emb_changes() || this._has_sam_changes() || this._has_final_rollup_changes();
		const is_saving = !!this.state.saving_adjustments;
		$btn.prop('disabled', is_saving || !has_changes);
		$btn.text(is_saving ? 'Saving…' : 'Save Changes');
	}

	_refresh_section_calc(idx) {
		const sections = this._get_costing_sections();
		if (idx < 0 || idx >= sections.length) return;
		const section = sections[idx];
		const data = section.costing || {};
		const cpk = data.cost_per_kg || {};
		const breakup = cpk.breakup || {};
		const heads = this._section_head_keys();
		let has_any = false;
		let adj_gross = 0;
		heads.forEach((key) => {
			const raw = (breakup[key] || {}).adjusted_cost_per_kg;
			if (!this._is_blankish(raw)) {
				adj_gross += this._num(raw);
				has_any = true;
			} else {
				adj_gross += this._num((breakup[key] || {}).cost_per_kg);
			}
		});
		const loss_pct = !this._is_blankish(data.loss_percent) ? this._num(data.loss_percent) : null;
		const adj_loss = (has_any && loss_pct != null) ? adj_gross * (loss_pct / 100) : null;
		const adj_total = has_any ? adj_gross + (adj_loss || 0) : null;

		const sum_of_heads = heads.reduce((a, k) => a + this._num((breakup[k] || {}).cost_per_kg), 0);
		const sys_gross = this._num(cpk.gross_total_cost) || sum_of_heads;
		const sys_total = this._num(cpk.total_fabric_cost);

		const $calc = this.$els.detail.find(`.tcwb-fabric-calc-split[data-section-idx="${idx}"]`);
		const fmt = (v) => (v === null || v === undefined) ? '' : this._fmt_currency(v);

		const set_cls = ($el, adj, sys) => {
			$el.removeClass('is-pos is-neg is-equal');
			if (adj === null || adj === undefined) return;
			if (Math.abs(adj - sys) < 0.0001) {
				$el.addClass('is-equal');
				return;
			}
			$el.addClass(adj > sys ? 'is-pos' : 'is-neg');
		};

		const $g = $calc.find('[data-role="fabric-adj-gross"]');
		const $l = $calc.find('[data-role="fabric-adj-loss"]');
		const $t = $calc.find('[data-role="fabric-adj-total"]');
		$g.text(fmt(has_any ? adj_gross : null));
		$l.text(fmt(adj_loss));
		$t.text(fmt(adj_total));
		set_cls($g, has_any ? adj_gross : null, sys_gross);
		set_cls($t, adj_total, sys_total);

		$calc.find('.tcwb-calc-row.total').toggleClass('has-adjusted', has_any);
	}

	_refresh_section_header_metrics() {
		const sections = this._get_costing_sections();
		if (!sections.length) return;
		let grand_cost_per_piece = 0;
		sections.forEach((section, idx) => {
			const base = this._section_effective_base_total(section);
			const total = this._section_total_for_display(section);
			const delta = total - base;
			const cost_per_piece = this._section_cost_per_piece(section);
			grand_cost_per_piece += cost_per_piece;
			const $section = this.$els.detail.find(`.tcwb-section[data-section-idx="${idx}"]`);
			if (!$section.length) return;

			$section.find('[data-role="section-total-per-kg"]').text(this._fmt_currency(base));
			$section.find('[data-role="section-total-value"]').text(this._fmt_currency(total));
			const $delta = $section.find('[data-role="section-total-delta"]');
			$delta
				.text(this._fmt_signed_currency(delta))
				.removeClass('is-pos is-neg')
				.addClass(delta > 0 ? 'is-pos' : (delta < 0 ? 'is-neg' : ''));
			$section.find('[data-role="section-cost-per-piece"]').text(this._fmt_currency(cost_per_piece));
		});
		this.$els.detail.find('[data-role="fabric-grand-cost-per-piece"]').text(this._fmt_currency(grand_cost_per_piece));
		this._recompute_final_rollup_panel();
	}

	// ---------- format helpers ----------
	_num(v) {
		if (v === null || v === undefined || v === '') return 0;
		const n = Number(v);
		return isFinite(n) ? n : 0;
	}

	_fmt_currency(v, decimals = 2) {
		if (v === null || v === undefined || v === '') return '—';
		const n = Number(v);
		if (!isFinite(n)) return '—';
		return `₹${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: decimals })}`;
	}

	_fmt_signed_currency(v) {
		if (v === null || v === undefined || v === '') return '—';
		const n = Number(v);
		if (!isFinite(n)) return '—';
		if (Math.abs(n) < 0.0001) return this._fmt_currency(0);
		const sign = n > 0 ? '+' : '-';
		return `${sign}${this._fmt_currency(Math.abs(n))}`;
	}

	_fmt_adjustment_input(v) {
		const n = this._normalize_adjustment(v);
		return Number.isInteger(n) ? String(n) : n.toFixed(2);
	}

	_fmt_percent(v) {
		if (v === null || v === undefined || v === '') return '—';
		const n = Number(v);
		if (!isFinite(n)) return '—';
		return `${n.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 })}%`;
	}

	_fmt_date(v) {
		if (!v) return '';
		try {
			const d = new Date(String(v).replace(' ', 'T'));
			if (isNaN(d.getTime())) return String(v);
			return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: '2-digit' });
		} catch (e) {
			return String(v);
		}
	}

	_safe_text(v) {
		if (this._is_blankish(v)) return '<span class="tcwb-muted">—</span>';
		return frappe.utils.escape_html(String(v));
	}

	_short_owner(owner) {
		if (!owner) return '—';
		const at = owner.indexOf('@');
		return at > 0 ? owner.slice(0, at) : owner;
	}

		_humanize(key) {
			return String(key).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
		}

	_is_gsm_mismatch(gsm, gsm_needed) {
			if (this._is_blankish(gsm) || this._is_blankish(gsm_needed)) return false;
			const a = Number(gsm);
			const b = Number(gsm_needed);
			if (isFinite(a) && isFinite(b)) return Math.abs(a - b) > 0.0001;
			return String(gsm).trim() !== String(gsm_needed).trim();
		}

	_is_blankish(v) {
		if (v === null || v === undefined) return true;
		if (typeof v === 'string') {
			const s = v.trim().toLowerCase();
			return s === '' || s === 'none' || s === 'null' || s === 'nan';
		}
		return false;
	}
	};
