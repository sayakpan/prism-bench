// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.listview_settings["Moodboard Dyed Fabric"] = {
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

		const run = (names, enqueue, force, useLlm) => {
			frappe.call({
				method: "prism.api.moodboard_fabric_matcher.bulk_match_closest_fabric",
				args: {
					names: names && names.length ? JSON.stringify(names) : null,
					enqueue: enqueue ? 1 : 0,
					force: force ? 1 : 0,
					use_llm: useLlm ? 1 : 0,
				},
				freeze: true,
				freeze_message: useLlm
					? __("Finding closest Fabric Master (LLM assisted)…")
					: __("Finding closest Fabric Master…"),
				callback: (r) => {
					const m = r.message || {};

					if (m.enqueued) {
						frappe.msgprint({
							title: __("Started"),
							indicator: "blue",
							message: useLlm
								? __(
										"Matching {0} row(s) in the background, LLM assisted. This is the slow " +
											"pass: Claude re-ranks the rows where the leader is unclear, and each " +
											"matched fabric is costed. Check Error Log (method " +
											"moodboard_fabric_matcher.bulk_match) for any failures.",
										[m.total]
								  )
								: __(
										"Matching {0} row(s) in the background, deterministic only — no model " +
											"calls and no costing. Rows sharing a clean quality, clean blend and " +
											"GSM are matched once and the result copied across, so this finishes " +
											"well before the row count suggests.",
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

		// --- Bulk: the buttons this list is here for ---
		// Two passes, cheap first. Deterministic puts a defensible match on every
		// row in seconds; the LLM pass revisits them and is expected to be run
		// separately, later.
		const bulk = (useLlm, allPrompt) => {
			const selected = listview.get_checked_items(true); // docnames only

			if (selected && selected.length) {
				// Inline for a handful so the result is visible immediately;
				// backgrounded once the wait would outlast the request. The LLM
				// pass costs a model call and a fabric costing per distinct
				// quality/blend/GSM, so it goes to the queue far sooner.
				run(selected, selected.length > (useLlm ? 20 : 500), false, useLlm);
				return;
			}

			frappe.confirm(allPrompt, () => run(null, true, false, useLlm));
		};

		listview.page.add_inner_button(
			__("Deterministic (fast)"),
			() =>
				bulk(
					false,
					__(
						"No rows selected. Find the closest Fabric Master for ALL Moodboard Dyed " +
							"Fabric rows, deterministically? No model calls and no fabric costing. " +
							"Rows already matched against their current clean quality, clean blend and " +
							"GSM are left alone, as are matches set by hand and rows already matched " +
							"with LLM assistance. Runs in the background."
					)
				),
			__("Find Closest Fabric Master")
		);

		listview.page.add_inner_button(
			__("LLM Assisted (slow)"),
			() =>
				bulk(
					true,
					__(
						"No rows selected. Find the closest Fabric Master for ALL Moodboard Dyed " +
							"Fabric rows with LLM assistance? Claude re-ranks the rows where the " +
							"leader is unclear and every matched fabric is costed, so this takes " +
							"hours where the deterministic pass takes seconds. Rows already matched " +
							"this way against their current columns are left alone, as are matches " +
							"set by hand. Runs in the background."
					)
				),
			__("Find Closest Fabric Master")
		);

		// --- Cost the linked fabric, without touching the match itself ---
		// These are inner buttons rather than action items because an action
		// item only renders once rows are checked, which put the "cost every
		// uncosted row" path -- the one you actually want after a deterministic
		// sweep -- out of reach.
		const recost = (ids, enqueue, force) => {
			frappe.call({
				method: "prism.api.moodboard_fabric_matcher.refresh_fabric_costs",
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
									"finishing processes, then caches them on the Fabric Master), and " +
									"much quicker for every row that shares it afterwards.",
								[m.total]
							),
						});
						return;
					}
					frappe.msgprint({
						title: __("Costs calculated"),
						indicator: m.failed ? "orange" : "green",
						message: __(
							"Processed {0}: costed {1}, already costed {2}, no match to cost {3}, failed {4}.",
							[m.total, m.costed, m.unchanged, m.skipped, m.failed]
						),
					});
					listview.clear_checked_items();
					listview.refresh();
				},
			});
		};

		listview.page.add_inner_button(
			__("Calculate Cost"),
			() => {
				const names = listview.get_checked_items(true);

				if (names && names.length) {
					frappe.confirm(
						__("Cost the Fabric Master behind {0} selected row(s)?", [names.length]),
						() => recost(names, names.length > 20, false)
					);
					return;
				}

				frappe.confirm(
					__(
						"No rows selected. Cost the matched Fabric Master for ALL rows that do not " +
							"have a cost yet? Rows already costed are left alone. Runs in the background."
					),
					() => recost(null, true, false)
				);
			},
			__("Fabric Master Cost")
		);

		listview.page.add_inner_button(
			__("Recalculate Cost (force)"),
			() => {
				const names = listview.get_checked_items(true);

				if (names && names.length) {
					frappe.confirm(
						__(
							"Recost {0} selected row(s), including those that already carry a cost?",
							[names.length]
						),
						() => recost(names, names.length > 20, true)
					);
					return;
				}

				frappe.confirm(
					__(
						"No rows selected. Recost EVERY matched row, including those that already " +
							"carry a cost? Runs in the background."
					),
					() => recost(null, true, true)
				);
			},
			__("Fabric Master Cost")
		);

		// --- Re-match, ignoring both the cache and any manual pick ---
		const forceRematch = (useLlm) => {
			const names = listview.get_checked_items(true);
			if (!names || !names.length) {
				frappe.msgprint(__("Select at least one row first."));
				return;
			}

			frappe.confirm(
				__(
					"Recompute the closest Fabric Master for {0} selected row(s), {1}? " +
						"This OVERWRITES matches that were set by hand.",
					[names.length, useLlm ? __("LLM assisted") : __("deterministic only")]
				),
				() => run(names, names.length > (useLlm ? 20 : 500), true, useLlm)
			);
		};

		listview.page.add_action_item(
			__("Re-match Closest Fabric Master (force, deterministic)"),
			() => forceRematch(false)
		);
		listview.page.add_action_item(
			__("Re-match Closest Fabric Master (force, LLM assisted)"),
			() => forceRematch(true)
		);
	},
};
