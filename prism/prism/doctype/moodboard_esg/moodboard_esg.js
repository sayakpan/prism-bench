// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.ui.form.on("Moodboard ESG", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;

		frm.add_custom_button(__("Calculate ESG"), () => {
			// Score the Main Body element (fall back to the first element).
			const elements = frm.doc.elements || [];
			const el =
				elements.find(
					(r) => (r.section_name || "").trim().toLowerCase() === "main body"
				) || elements[0];

			const consumption = el ? flt(el.consumption) : 0;
			const marker = el ? flt(el.marker_efficiency) : 0;

			// Guard: don't call the rating engine without real inputs.
			if (!(consumption > 0) || !(marker > 0)) {
				frappe.msgprint({
					title: __("Cannot calculate"),
					indicator: "orange",
					message: __(
						"Consumption and Marker Efficiency must both be greater than 0 on the Main Body element."
					),
				});
				return;
			}

			frappe.call({
				method: "prism.api.esg.calculate_esg_rating",
				args: { esg_id: frm.doc.name },
				freeze: true,
				freeze_message: __("Calculating ESG rating…"),
				callback: (r) => {
					const m = r.message || {};
					if (!m.success) return; // errors surface via the server's own popup
					const rt = m.rating || {};
					frappe.msgprint({
						title: __("ESG calculated"),
						indicator: "green",
						message: __("Rating: {0} · Score: {1}% · Category: {2}", [
							rt.overallRating != null ? rt.overallRating : "—",
							rt.scorePercent != null ? rt.scorePercent : "—",
							rt.category || "—",
						]),
					});
					frm.reload_doc();
				},
			});
		});
	},
});
