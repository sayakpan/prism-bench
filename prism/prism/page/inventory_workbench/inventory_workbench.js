frappe.provide('prism');

frappe.pages['inventory-workbench'].on_page_load = function(wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Inventory Workbench',
		single_column: true,
	});
	wrapper.inventory_workbench = new prism.InventoryWorkbench(page, wrapper);
};

prism.InventoryWorkbench = class InventoryWorkbench {
	constructor(page, wrapper) {
		this.page = page;
		this.$body = $(wrapper).find('.layout-main-section');
		this.$body.addClass('inv-wb');

		this.state = {
			tab: 'list',
			filters: {},
			search: '',
			sort_by: 'creation',
			sort_order: 'desc',
			page: 1,
			page_size: 25,
			session_id: null,
		};
		this.filter_options = {};
		this.list_loaded = false;
		this.qna_loaded = false;

		this._read_hash();
		this._build_shell();
		this._activate_tab(this.state.tab, true);
	}

	// ---------- shell ----------

	_build_shell() {
		this.$body.html(`
			<div class="inv-wb-topbar">
				<ul class="nav nav-tabs inv-wb-tabs" role="tablist">
					<li class="nav-item"><a class="nav-link" data-tab="list">Inventory</a></li>
					<li class="nav-item"><a class="nav-link" data-tab="qna">Ask AI</a></li>
				</ul>
				<a class="btn btn-primary btn-sm inv-wb-add" href="/app/inventory/new" target="_blank" rel="noopener">+ Add Inventory</a>
			</div>
			<div class="inv-wb-tab-body" data-tab-body="list" style="display:none;"></div>
			<div class="inv-wb-tab-body" data-tab-body="qna" style="display:none;"></div>
		`);

		this.$body.find('.inv-wb-tabs .nav-link').on('click', (e) => {
			const tab = $(e.currentTarget).attr('data-tab');
			this._activate_tab(tab);
		});
	}

	_activate_tab(tab, initial = false) {
		this.state.tab = tab;
		this.$body.find('.inv-wb-tabs .nav-link').removeClass('active');
		this.$body.find(`.inv-wb-tabs .nav-link[data-tab="${tab}"]`).addClass('active');
		this.$body.find('.inv-wb-tab-body').hide();
		this.$body.find(`.inv-wb-tab-body[data-tab-body="${tab}"]`).show();

		if (tab === 'list' && !this.list_loaded) {
			this._render_list_tab();
			this.list_loaded = true;
		} else if (tab === 'qna' && !this.qna_loaded) {
			this._render_qna_tab();
			this.qna_loaded = true;
		}

		if (!initial) this._write_hash();
	}

	_read_hash() {
		const hash = (window.location.hash || '').replace(/^#/, '');
		if (!hash) return;
		const params = new URLSearchParams(hash);
		const tab = params.get('tab');
		if (tab === 'list' || tab === 'qna') this.state.tab = tab;
		const session = params.get('session');
		if (session) this.state.session_id = session;
	}

	_write_hash() {
		const params = new URLSearchParams();
		params.set('tab', this.state.tab);
		if (this.state.session_id) params.set('session', this.state.session_id);
		window.history.replaceState(null, '', '#' + params.toString());
	}

	// ================================================================
	//  TAB 1: LISTING
	// ================================================================

	_render_list_tab() {
		const $panel = this.$body.find('.inv-wb-tab-body[data-tab-body="list"]');
		$panel.html(`
			<div class="inv-wb-cards" data-role="cards"></div>
			<div class="inv-wb-filters" data-role="filters"></div>
			<div class="inv-wb-filters-row2" data-role="filters-row2"></div>
			<div class="inv-wb-table-wrap">
				<div data-role="table"><div class="inv-wb-empty">Loading…</div></div>
				<div class="inv-wb-pager" data-role="pager"></div>
			</div>
		`);
		this.$list = {
			cards: $panel.find('[data-role="cards"]'),
			filters: $panel.find('[data-role="filters"]'),
			filtersRow2: $panel.find('[data-role="filters-row2"]'),
			table: $panel.find('[data-role="table"]'),
			pager: $panel.find('[data-role="pager"]'),
		};

		this._fetch_summary();
		this._fetch_filter_options().then(() => {
			this._build_filter_bar();
			this._fetch_list();
		});
	}

	_fetch_summary() {
		frappe.call({
			method: 'prism.api.inventory.get_inventory_summary',
		}).then((r) => {
			const res = r.message || {};
			if (!res.status) {
				this.$list.cards.html(`<div class="inv-wb-empty">${frappe.utils.escape_html(res.error || 'Failed to load summary.')}</div>`);
				return;
			}
			const cards = (res.data && res.data.cards) || [];
			this.$list.cards.html(cards.map((c) => `
				<div class="inv-wb-card">
					<div class="inv-wb-card-label">${frappe.utils.escape_html(c.label)}</div>
					<div class="inv-wb-card-value">${this._fmt_number(c.value)}</div>
				</div>
			`).join(''));
		});
	}

	_fetch_filter_options() {
		return frappe.call({
			method: 'prism.api.inventory.get_inventory_filter_options',
		}).then((r) => {
			const res = r.message || {};
			this.filter_options = (res.status && res.data) || {};
		});
	}

	_build_filter_bar() {
		const $f = this.$list.filters;
		$f.empty();

		const specs = [
			{ key: 'material_type', label: 'Material Type' },
			{ key: 'plant', label: 'Plant' },
			{ key: 'division', label: 'Division' },
			{ key: 'overall_status', label: 'Status' },
			{ key: 'storage_location', label: 'Storage' },
			{ key: 'inventory_type', label: 'Inventory Type' },
			{ key: 'material_group', label: 'Material Group' },
		];

		specs.forEach((spec) => {
			const opts = this.filter_options[spec.key] || [];
			const selected = this.state.filters[spec.key] || [];
			const items_html = opts.map((v) => {
				const val = typeof v === 'object' ? v.value : v;
				const label = typeof v === 'object' ? v.label : v;
				const checked = selected.includes(val) ? 'checked' : '';
				return `<label class="inv-wb-ms-item">
					<input type="checkbox" value="${frappe.utils.escape_html(val)}" ${checked}>
					<span>${frappe.utils.escape_html(label)}</span>
				</label>`;
			}).join('');
			const summary = selected.length ? `${selected.length} selected` : 'All';
			const hasClass = selected.length ? ' has-selection' : '';
			const $wrap = $(`
				<div class="form-group frappe-control inv-wb-ms${hasClass}" style="margin-bottom:0;" data-filter-key="${spec.key}">
					<label class="control-label" style="font-size:11px;margin-bottom:4px;">${frappe.utils.escape_html(spec.label)}</label>
					<div class="inv-wb-ms-trigger form-control input-sm">${frappe.utils.escape_html(summary)}</div>
					<div class="inv-wb-ms-dropdown" style="display:none;">
						<div class="inv-wb-ms-list">${items_html}</div>
					</div>
				</div>
			`);
			$f.append($wrap);
		});

		const $row2 = this.$list.filtersRow2;
		$row2.empty();

		const $search = $(`
			<div class="form-group frappe-control inv-wb-search" style="margin-bottom:0;">
				<input type="text" class="form-control input-sm" data-role="search" placeholder="Search material / description / customer / batch…" value="${frappe.utils.escape_html(this.state.search)}">
			</div>
		`);
		$row2.append($search);

		const $reset = $(`<a class="inv-wb-reset" href="#">Reset</a>`);
		$row2.append($reset);

		$f.on('click', '.inv-wb-ms-trigger', (e) => {
			const $ms = $(e.currentTarget).closest('.inv-wb-ms');
			const $dd = $ms.find('.inv-wb-ms-dropdown');
			const wasOpen = $dd.is(':visible');
			$f.find('.inv-wb-ms-dropdown').hide();
			if (!wasOpen) $dd.show();
		});

		$f.on('change', '.inv-wb-ms-item input', (e) => {
			const $ms = $(e.target).closest('.inv-wb-ms');
			const key = $ms.attr('data-filter-key');
			const vals = [];
			$ms.find('.inv-wb-ms-item input:checked').each(function() { vals.push($(this).val()); });
			if (vals.length) {
				this.state.filters[key] = vals;
			} else {
				delete this.state.filters[key];
			}
			$ms.toggleClass('has-selection', vals.length > 0);
			$ms.find('.inv-wb-ms-trigger').text(vals.length ? `${vals.length} selected` : 'All');
			this.state.page = 1;
			this._fetch_list();
		});

		$(document).on('click.inv-wb-ms', (e) => {
			if (!$(e.target).closest('.inv-wb-ms').length) {
				$f.find('.inv-wb-ms-dropdown').hide();
			}
		});

		let debounce_timer = null;
		$row2.on('input', 'input[data-role="search"]', (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(debounce_timer);
			debounce_timer = setTimeout(() => {
				this.state.search = val;
				this.state.page = 1;
				this._fetch_list();
			}, 350);
		});

		$reset.on('click', (e) => {
			e.preventDefault();
			this.state.filters = {};
			this.state.search = '';
			this.state.page = 1;
			this._build_filter_bar();
			this._fetch_list();
		});

		requestAnimationFrame(() => {
			const $last = $f.find('.inv-wb-ms:last');
			if ($last.length) {
				const fLeft = $f[0].getBoundingClientRect().left;
				const lRight = $last[0].getBoundingClientRect().right;
				$search.css('max-width', (lRight - fLeft) + 'px');
			}
		});
	}

	_fetch_list() {
		this.$list.table.html('<div class="inv-wb-empty">Loading…</div>');
		frappe.call({
			method: 'prism.api.inventory.get_inventory_list',
			args: {
				filters: this.state.filters,
				search: this.state.search,
				sort_by: this.state.sort_by,
				sort_order: this.state.sort_order,
				page: this.state.page,
				page_size: this.state.page_size,
			},
		}).then((r) => {
			const res = r.message || {};
			if (!res.status) {
				this.$list.table.html(`<div class="inv-wb-empty">${frappe.utils.escape_html(res.error || 'Failed to load list.')}</div>`);
				this.$list.pager.empty();
				return;
			}
			this._render_table(res.data);
			this._render_pager(res.data);
		});
	}

	_render_table(data) {
		const rows = data.rows || [];
		if (!rows.length) {
			this.$list.table.html('<div class="inv-wb-empty">No inventory records match these filters.</div>');
			return;
		}

		const cols = [
			{ key: 'stock_material', label: 'Material', sortable: true },
			{ key: 'material_description', label: 'Description', sortable: false },
			{ key: 'material_type', label: 'Type', sortable: true },
			{ key: 'stock_quantity', label: 'Quantity', sortable: true, num: true },
			{ key: 'base_unit_of_measure', label: 'UoM', sortable: false },
			{ key: 'plant', label: 'Plant', sortable: true },
			{ key: 'storage_location', label: 'Storage', sortable: false },
			{ key: 'overall_status', label: 'Status', sortable: true },
		];

		const { sort_by, sort_order } = this.state;
		const head = cols.map((c) => {
			const active = c.sortable && c.key === sort_by;
			const indicator = !c.sortable ? '' : (active
				? (sort_order === 'asc' ? '▲' : '▼')
				: '↕');
			const align = c.num ? 'text-align:right;' : '';
			return `<th data-col="${c.key}" data-sortable="${c.sortable}" class="${active ? 'active' : ''}" style="${align}">
				${frappe.utils.escape_html(c.label)}<span class="sort-ind">${indicator}</span>
			</th>`;
		}).join('');

		const body = rows.map((row) => `
			<tr data-name="${frappe.utils.escape_html(row.name)}">
				${cols.map((c) => {
					let v = row[c.key];
					if (c.key === 'material_type' && v && row.material_type_description) {
						v = `${v} (${row.material_type_description})`;
					}
					const empty = v === null || v === undefined || v === '';
					const text = empty ? '—' : (c.num ? this._fmt_number(v) : v);
					const align = c.num ? 'text-align:right;' : '';
					if (c.key === 'stock_material' && row.name && v) {
						const href = `/app/inventory/${encodeURIComponent(row.name)}`;
						return `<td style="${align}"><a href="${href}" target="_blank" rel="noopener">${frappe.utils.escape_html(String(text))}</a></td>`;
					}
					return `<td style="${align}">${frappe.utils.escape_html(String(text))}</td>`;
				}).join('')}
			</tr>
		`).join('');

		this.$list.table.html(`
			<table class="inv-wb-table">
				<thead><tr>${head}</tr></thead>
				<tbody>${body}</tbody>
			</table>
		`);

		this.$list.table.find('th[data-sortable="true"]').on('click', (e) => {
			const col = $(e.currentTarget).attr('data-col');
			if (this.state.sort_by === col) {
				this.state.sort_order = this.state.sort_order === 'asc' ? 'desc' : 'asc';
			} else {
				this.state.sort_by = col;
				this.state.sort_order = 'asc';
			}
			this._fetch_list();
		});

		this.$list.table.find('tbody tr').on('dblclick', (e) => {
			const name = $(e.currentTarget).attr('data-name');
			if (name) frappe.set_route('Form', 'Inventory', name);
		});
	}

	_render_pager(data) {
		const total = data.total || 0;
		const page = data.page || 1;
		const size = data.page_size || 25;
		const total_pages = Math.max(1, Math.ceil(total / size));
		const from = total === 0 ? 0 : (page - 1) * size + 1;
		const to = Math.min(total, page * size);

		this.$list.pager.html(`
			<div>Showing <b>${from}–${to}</b> of <b>${this._fmt_number(total)}</b></div>
			<div>
				<button class="btn btn-default btn-sm" data-role="prev" ${page <= 1 ? 'disabled' : ''}>‹ Prev</button>
				<span style="margin:0 8px;">Page ${page} / ${total_pages}</span>
				<button class="btn btn-default btn-sm" data-role="next" ${page >= total_pages ? 'disabled' : ''}>Next ›</button>
				<select class="form-control input-sm" data-role="page-size" style="display:inline-block;width:auto;margin-left:12px;">
					${[10, 25, 50, 100, 200].map((n) => `<option value="${n}" ${n === size ? 'selected' : ''}>${n} / page</option>`).join('')}
				</select>
			</div>
		`);

		this.$list.pager.find('[data-role="prev"]').on('click', () => {
			if (this.state.page > 1) { this.state.page -= 1; this._fetch_list(); }
		});
		this.$list.pager.find('[data-role="next"]').on('click', () => {
			if (this.state.page < total_pages) { this.state.page += 1; this._fetch_list(); }
		});
		this.$list.pager.find('[data-role="page-size"]').on('change', (e) => {
			this.state.page_size = parseInt($(e.currentTarget).val(), 10);
			this.state.page = 1;
			this._fetch_list();
		});
	}

	_fmt_number(n) {
		if (n === null || n === undefined) return '—';
		try { return Number(n).toLocaleString(); } catch (e) { return String(n); }
	}

	// ================================================================
	//  TAB 2: AI QnA
	// ================================================================

	_render_qna_tab() {
		const $panel = this.$body.find('.inv-wb-tab-body[data-tab-body="qna"]');
		$panel.html(`
			<div class="inv-wb-qna">
				<div class="inv-wb-qna-starters" data-role="starters">
					<span class="inv-wb-starter" data-q="Top 10 materials by total stock quantity">Top 10 materials by total stock quantity</span>
					<span class="inv-wb-starter" data-q="How many items have stock quantity below 10?">How many items have stock quantity below 10?</span>
					<span class="inv-wb-starter" data-q="Customers having the most t-shirt stock">Customers having the most t-shirt stock</span>
					<span class="inv-wb-starter" data-q="Storage locations with the least swaetshirt stock">Storage locations with the least swaetshirt stock</span>
					<span class="inv-wb-starter" data-q="Which material types have the most stock?">Which material types have the most stock?</span>
				</div>
				<div class="inv-wb-qna-messages" data-role="messages">
					<div class="inv-wb-empty">Ask a question about inventory — results are generated from live data.</div>
				</div>
				<div class="inv-wb-qna-input">
					<textarea class="form-control" data-role="question" placeholder="Ask about your inventory…"></textarea>
					<div style="display:flex;flex-direction:column;gap:4px;">
						<button class="btn btn-primary btn-sm" data-role="send">Send</button>
						<button class="btn btn-default btn-sm" data-role="new">New chat</button>
					</div>
				</div>
			</div>
		`);

		this.$qna = {
			messages: $panel.find('[data-role="messages"]'),
			question: $panel.find('[data-role="question"]'),
			send: $panel.find('[data-role="send"]'),
			starters: $panel.find('[data-role="starters"]'),
			newBtn: $panel.find('[data-role="new"]'),
		};

		this.$qna.send.on('click', () => this._send_question());
		this.$qna.question.on('keydown', (e) => {
			if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
				e.preventDefault();
				this._send_question();
			}
		});
		this.$qna.starters.on('click', '.inv-wb-starter', (e) => {
			const q = $(e.currentTarget).attr('data-q');
			this.$qna.question.val(q);
			this._send_question();
		});
		this.$qna.newBtn.on('click', () => {
			this.state.session_id = null;
			this._write_hash();
			this.$qna.messages.empty().append('<div class="inv-wb-empty">New chat. Ask a question.</div>');
		});

		if (this.state.session_id) {
			this._load_qna_session();
		}
	}

	_load_qna_session() {
		frappe.call({
			method: 'prism.api.inventory_qna.get_session',
			args: { session_id: this.state.session_id },
		}).then((r) => {
			const res = r.message || {};
			if (!res.status) return;
			this.$qna.messages.empty();
			(res.data.turns || []).forEach((t) => {
				this._append_user_msg(t.question);
				if (t.error) {
					this._append_bot_msg({ error: t.error });
				} else {
					this._append_bot_msg({
						answer: t.answer,
						sql: t.sql,
						rationale: t.rationale,
						row_count: t.row_count,
						truncated: t.truncated,
					});
				}
			});
			this._scroll_messages();
		});
	}

	_send_question() {
		const q = (this.$qna.question.val() || '').trim();
		if (!q) return;
		this.$qna.question.val('');
		this.$qna.send.prop('disabled', true).text('Thinking…');

		if (this.$qna.messages.find('.inv-wb-empty').length) {
			this.$qna.messages.empty();
		}

		this._append_user_msg(q);
		const $typing = $(`<div class="inv-wb-msg inv-wb-msg-bot"><div class="inv-wb-typing">Querying Inventory data…</div></div>`);
		this.$qna.messages.append($typing);
		this._scroll_messages();

		frappe.call({
			method: 'prism.api.inventory_qna.ask',
			args: { question: q, session_id: this.state.session_id },
		}).then((r) => {
			$typing.remove();
			this.$qna.send.prop('disabled', false).text('Send');
			const res = r.message || {};
			if (!res.status) {
				this._append_bot_msg({ error: res.error || 'Something went wrong.' });
				return;
			}
			const d = res.data || {};
			if (d.session_id && d.session_id !== this.state.session_id) {
				this.state.session_id = d.session_id;
				this._write_hash();
			}
			this._append_bot_msg(d);
		}).catch((err) => {
			$typing.remove();
			this.$qna.send.prop('disabled', false).text('Send');
			this._append_bot_msg({ error: (err && err.message) || 'Request failed.' });
		});
	}

	_append_user_msg(text) {
		this.$qna.messages.append(`
			<div class="inv-wb-msg inv-wb-msg-user">
				<div class="inv-wb-msg-bubble">${frappe.utils.escape_html(text)}</div>
			</div>
		`);
		this._scroll_messages();
	}

	_append_bot_msg(payload) {
		const esc = frappe.utils.escape_html;
		if (payload.error) {
			this.$qna.messages.append(`
				<div class="inv-wb-msg inv-wb-msg-bot">
					<div class="inv-wb-msg-bubble error">${esc(payload.error)}</div>
				</div>
			`);
		} else {
			const meta_parts = [];
			if (typeof payload.row_count === 'number') {
				meta_parts.push(`${payload.row_count} row${payload.row_count === 1 ? '' : 's'}`);
			}
			if (payload.truncated) meta_parts.push('truncated');
			const meta = meta_parts.length ? `<div class="inv-wb-msg-meta">${meta_parts.join(' · ')}</div>` : '';
			const trace = payload.rationale ? `
				<details class="inv-wb-msg-trace">
					<summary>How I got this</summary>
					<div style="margin-top:6px;font-size:12px;">${esc(payload.rationale)}</div>
				</details>
			` : '';
			const answer_html = esc(payload.answer || '').replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>');
			this.$qna.messages.append(`
				<div class="inv-wb-msg inv-wb-msg-bot">
					<div class="inv-wb-msg-bubble">${answer_html}</div>
					${meta}
					${trace}
				</div>
			`);
		}
		this._scroll_messages();
	}

	_scroll_messages() {
		const el = this.$qna.messages[0];
		if (el) el.scrollTop = el.scrollHeight;
	}
};
