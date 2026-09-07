// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.listview_settings["Surplus Stock"] = {
	add_fields: ["closest_fabric_master", "closest_match_score", "closest_match_method"],

	get_indicator(doc) {
		if (!doc.closest_fabric_master) return [__("Unmatched"), "gray", "closest_fabric_master,is,not set"];
		if (doc.closest_match_method === "Manual")
			return [__("Matched (manual)"), "blue", "closest_match_method,=,Manual"];
		const score = doc.closest_match_score || 0;
		if (score >= 80) return [__("Matched {0}", [Math.round(score)]), "green", "closest_fabric_master,is,set"];
		if (score >= 60) return [__("Weak match {0}", [Math.round(score)]), "orange", "closest_fabric_master,is,set"];
		return [__("Poor match {0}", [Math.round(score)]), "red", "closest_fabric_master,is,set"];
	},

	onload(listview) {
		if (!frappe.user.has_role("System Manager")) return;

		const run = (names, enqueue, force) => {
			frappe.call({
				method: "prism.api.fabric_matcher.bulk_match_closest_fabric",
				args: {
					names: names && names.length ? JSON.stringify(names) : null,
					enqueue: enqueue ? 1 : 0,
					force: force ? 1 : 0,
				},
				freeze: true,
				freeze_message: __("Finding closest Fabric Master…"),
				callback: (r) => {
					const m = r.message || {};

					if (m.enqueued) {
						frappe.msgprint({
							title: __("Started"),
							indicator: "blue",
							message: __(
								"Matching {0} row(s) in the background. Rows sharing a quality, " +
									"blend and GSM are matched once and the result copied across, so " +
									"this finishes well before the row count suggests. Check Error Log " +
									"(method fabric_matcher.bulk_match) for any failures.",
								[m.total]
							),
						});
						return;
					}

					frappe.msgprint({
						title: __("Matching complete"),
						indicator: m.failed ? "orange" : "green",
						message: __(
							"Processed {0}: matched {1}, already current {2}, skipped {3}, failed {4}.",
							[m.total, m.matched, m.unchanged, m.skipped, m.failed]
						),
					});
					listview.clear_checked_items();
					listview.refresh();
				},
			});
		};

		// --- Bulk: the button this list is here for ---
		listview.page.add_inner_button(__("Find Closest Fabric Master"), () => {
			const selected = listview.get_checked_items(true); // docnames only

			if (selected && selected.length) {
				// Inline for a handful so the result is visible immediately;
				// backgrounded once the wait would outlast the request.
				run(selected, selected.length > 100, false);
				return;
			}

			frappe.confirm(
				__(
					"No rows selected. Find the closest Fabric Master for ALL Surplus Stock rows? " +
						"Rows already matched against their current quality, blend and GSM are left " +
						"alone, as are matches set by hand. Runs in the background."
				),
				() => run(null, true, false)
			);
		});

		// --- Recost the linked fabric, without touching the match itself ---
		listview.page.add_action_item(__("Refresh Fabric Master Cost"), () => {
			const names = listview.get_checked_items(true);

			const recost = (ids, enqueue, force) => {
				frappe.call({
					method: "prism.api.fabric_matcher.refresh_fabric_costs",
					args: {
						names: ids && ids.length ? JSON.stringify(ids) : null,
						enqueue: enqueue ? 1 : 0,
						force: force ? 1 : 0,
					},
					freeze: true,
					freeze_message: __("Costing matched fabrics…"),
					callback: (r) => {
						const m = r.message || {};
						if (m.enqueued) {
							frappe.msgprint({
								title: __("Started"),
								indicator: "blue",
								message: __(
									"Costing the fabrics behind {0} row(s) in the background. The first " +
										"costing of each fabric is slow (it matches the knitting code and " +
										"finishing processes, then caches them on the Fabric Master).",
									[m.total]
								),
							});
							return;
						}
						frappe.msgprint({
							title: __("Costs refreshed"),
							indicator: m.failed ? "orange" : "green",
							message: __(
								"Processed {0}: costed {1}, already costed {2}, no match to cost {3}, failed {4}.",
								[m.total, m.costed, m.unchanged, m.skipped, m.failed]
							),
						});
						listview.refresh();
					},
				});
			};

			if (names && names.length) {
				frappe.confirm(
					__("Recost the Fabric Master behind {0} selected row(s)?", [names.length]),
					() => recost(names, names.length > 20, true)
				);
				return;
			}

			frappe.confirm(
				__(
					"No rows selected. Cost the matched Fabric Master for ALL rows that do not " +
						"have a cost yet? Runs in the background."
				),
				() => recost(null, true, false)
			);
		});

		// --- Re-match, ignoring both the cache and any manual pick ---
		listview.page.add_action_item(__("Re-match Closest Fabric Master (force)"), () => {
			const names = listview.get_checked_items(true);
			if (!names || !names.length) {
				frappe.msgprint(__("Select at least one row first."));
				return;
			}

			frappe.confirm(
				__(
					"Recompute the closest Fabric Master for {0} selected row(s)? " +
						"This OVERWRITES matches that were set by hand.",
					[names.length]
				),
				() => run(names, names.length > 100, true)
			);
		});
	},
};
