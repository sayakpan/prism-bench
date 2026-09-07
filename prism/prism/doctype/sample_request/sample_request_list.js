// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.listview_settings["Sample Request"] = {
	onload(listview) {
		if (!frappe.user.has_role("System Manager")) return;

		listview.page.add_inner_button(__("Feed ESG Input (bulk)"), () => {
			const selected = listview.get_checked_items(true); // docnames only

			const run = (names, enqueue) => {
				frappe.call({
					method: "prism.api.esg.feed_esg_input_bulk",
					args: {
						names: names && names.length ? JSON.stringify(names) : null,
						enqueue: enqueue ? 1 : 0,
					},
					freeze: true,
					freeze_message: __("Feeding ESG input…"),
					callback: (r) => {
						const m = r.message || {};
						if (m.enqueued) {
							frappe.msgprint({
								title: __("Started"),
								indicator: "blue",
								message: __(
									"Feeding ESG input for {0} sample(s) in the background. " +
										"Check Error Log (method feed_esg_input_bulk) for any failures.",
									[m.total]
								),
							});
							return;
						}
						frappe.msgprint({
							title: __("ESG input fed"),
							indicator: m.failed ? "orange" : "green",
							message: __(
								"Processed {0}: created {1}, skipped {2} (existing or missing links), failed {3}.",
								[m.total, m.created, m.skipped, m.failed]
							),
						});
						listview.refresh();
					},
				});
			};

			if (selected && selected.length) {
				// Selection: run inline for small batches, background for large ones.
				run(selected, selected.length > 200);
			} else {
				frappe.confirm(
					__(
						"No rows selected. Feed ESG input for ALL Sample Requests that have " +
							"both a Fabric Master and a Trim Costing? Runs in the background."
					),
					() => run(null, true)
				);
			}
		});
	},
};
