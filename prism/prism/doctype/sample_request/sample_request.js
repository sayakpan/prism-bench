// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.ui.form.on("Sample Request", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;

		// Feed the ESG input record from the sample's linked Fabric Master + Trim
		// Costing. Only offered once both links are set — the ESG needs their data.
		if (!frm.doc.fabric_master || !frm.doc.trim_costing) return;

		frm.add_custom_button(__("Feed ESG Input"), () => {
			frappe.call({
				method: "prism.api.esg.feed_esg_input_for_sample",
				args: { sample_id: frm.doc.name },
				freeze: true,
				freeze_message: __("Feeding ESG input…"),
				callback: (r) => {
					const m = r.message || {};
					if (!m.esg) {
						frappe.msgprint({
							title: __("Nothing to feed"),
							indicator: "orange",
							message: m.message || __("Set Fabric Master and Trim Costing first."),
						});
						return;
					}
					const link = `<a href="/app/moodboard-esg/${encodeURIComponent(m.esg)}">${frappe.utils.escape_html(m.esg)}</a>`;
					frappe.msgprint({
						title: m.created ? __("ESG input fed") : __("ESG input already exists"),
						indicator: "green",
						message: m.created
							? __("Fed ESG input record: {0}", [link])
							: __("This sample already has an ESG record: {0}", [link]),
					});
				},
			});
		});
	},
});
