// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.ui.form.on("Brand", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (!frappe.user.has_role("System Manager")) return;

		// --- Re-extract the brand theme from its website (LLM-backed) ---
		frm.add_custom_button(__("Update Theme"), () => {
			// The theme is derived from the site's HTML, so there's nothing to
			// run against without one.
			if (!frm.doc.website) {
				frappe.msgprint({
					title: __("Website required"),
					indicator: "orange",
					message: __("Set this brand's Website first — the theme is extracted from it."),
				});
				return;
			}

			const run = () => {
				frappe.call({
					method: "prism.api.brand.regenerate_brand_theme",
					args: { brand_id: frm.doc.name },
					freeze: true,
					freeze_message: __("Extracting the brand theme from the website…"),
					callback: (r) => {
						const m = r.message || {};
						if (m.success) {
							frappe.show_alert({ message: __("Brand theme updated"), indicator: "green" });
							frm.reload_doc();
						} else {
							frappe.msgprint({
								title: __("Could not update theme"),
								indicator: "red",
								message: m.error || __("Theme extraction failed."),
							});
						}
					},
				});
			};

			// A manual run always regenerates, so warn before discarding a theme
			// that is already there.
			if (frm.doc.brand_theme) {
				frappe.confirm(
					__("Regenerate the theme for {0} from {1}? The existing theme will be overwritten.", [
						frm.doc.brand || frm.doc.name,
						frm.doc.website,
					]),
					run
				);
			} else {
				run();
			}
		});
	},
});
