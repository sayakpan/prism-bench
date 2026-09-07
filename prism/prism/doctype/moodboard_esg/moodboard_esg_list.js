// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.listview_settings["Moodboard ESG"] = {
	onload(listview) {
		if (!frappe.user.has_role("System Manager")) return;

		// Completion summary from the background job (see api/esg.py).
		if (!listview._esg_bulk_bound) {
			listview._esg_bulk_bound = true;
			frappe.realtime.on("esg_bulk_rating_done", (d) => {
				d = d || {};
				frappe.msgprint({
					title: __("Bulk ESG calculation done"),
					indicator: d.failed || d.aborted ? "orange" : "green",
					message: [
						__("Processed {0}: scored {1}, skipped {2} (no consumption/marker), failed {3}.", [
							d.total,
							d.scored,
							d.skipped,
							d.failed,
						]),
						d.aborted
							? __("Stopped early — the rating service returned repeated errors.")
							: "",
					]
						.filter(Boolean)
						.join("<br>"),
				});
				listview.refresh();
			});
		}

		listview.page.add_inner_button(__("Calculate ESG (bulk)"), () => {
			const selected = listview.get_checked_items(true); // docnames only

			const start = (names) => {
				frappe.call({
					method: "prism.api.esg.calculate_esg_rating_bulk",
					args: {
						names: names && names.length ? JSON.stringify(names) : null,
						enqueue: 1, // always background — sequential ~10s/record
					},
					freeze: true,
					freeze_message: __("Queuing…"),
					callback: (r) => {
						const m = r.message || {};
						if (!m.total) {
							frappe.msgprint(__("Nothing to calculate."));
							return;
						}
						frappe.msgprint({
							title: __("Started"),
							indicator: "blue",
							message: __(
								"Calculating ratings for {0} record(s) in the background. The rating " +
									"service runs one at a time (~10s each), so this may take a while. " +
									"A progress bar shows live; you'll get a summary when it finishes.",
								[m.total]
							),
						});
					},
				});
			};

			if (selected && selected.length) {
				frappe.confirm(
					__(
						"Calculate ESG ratings for the {0} selected record(s)? Calls the rating " +
							"service sequentially (~10s each).",
						[selected.length]
					),
					() => start(selected)
				);
			} else {
				frappe.confirm(
					__(
						"No rows selected. Calculate ESG ratings for ALL Moodboard ESG records? " +
							"Calls the rating service sequentially (~10s each) and may take a while."
					),
					() => start(null)
				);
			}
		});
	},
};
