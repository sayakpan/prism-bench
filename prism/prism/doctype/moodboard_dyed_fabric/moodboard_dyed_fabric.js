// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.ui.form.on("Moodboard Dyed Fabric", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;

		const manual = frm.doc.closest_match_method === "Manual";

		frm.add_custom_button(
			manual ? __("Re-match (overwrite manual)") : __("Find Closest Fabric Master"),
			() => {
				const call = () =>
					frappe.call({
						method: "prism.api.moodboard_fabric_matcher.find_closest_fabric_master",
						args: { name: frm.doc.name, force: manual ? 1 : 0 },
						freeze: true,
						freeze_message: __("Finding closest Fabric Master…"),
						callback: (r) => {
							const m = r.message || {};
							if (!m.matched) {
								frappe.msgprint({
									title: __("No match"),
									indicator: "orange",
									message:
										m.reason ||
										__("No Fabric Master was close enough to this quality and blend."),
								});
								return;
							}
							frappe.show_alert({
								message: __("Matched {0} ({1}%)", [
									m.match.fabric_code || m.match.fabric_id,
									Math.round(m.match.score),
								]),
								indicator: "green",
							});
							frm.reload_doc();
						},
					});

				if (manual) {
					frappe.confirm(
						__("This match was set by hand. Recompute and overwrite it?"),
						call
					);
				} else {
					call();
				}
			},
			__("Fabric Master")
		);

		// Costing is a separate press because it is separately expensive: the
		// first costing of a fabric makes model calls to resolve its knitting
		// code and finishing processes. The bulk deterministic pass skips it
		// entirely, so most rows arrive here matched but not costed.
		if (!frm.doc.closest_fabric_master) return;

		frm.add_custom_button(
			frm.doc.closest_fabric_costed_on ? __("Recalculate Cost") : __("Calculate Cost"),
			() => {
				frappe.call({
					method: "prism.api.moodboard_fabric_matcher.calculate_fabric_cost",
					args: { name: frm.doc.name },
					freeze: true,
					freeze_message: __("Costing the matched fabric…"),
					callback: (r) => {
						const m = r.message || {};
						if (!m.costed) {
							frappe.msgprint({
								title: __("Not costed"),
								indicator: "orange",
								message: m.reason || __("The matched Fabric Master would not cost."),
							});
							frm.reload_doc();
							return;
						}
						frappe.show_alert({
							message: __("{0} per kg", [format_currency(m.cost)]),
							indicator: "green",
						});
						frm.reload_doc();
					},
				});
			},
			__("Fabric Master")
		);
	},
});
