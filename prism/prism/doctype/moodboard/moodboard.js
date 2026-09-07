// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.ui.form.on("Moodboard", {
	refresh(frm) {
		if (frm.is_new()) return;

		// --- Upgrade THIS board's thumbnail to high quality (System Manager) ---
		frm.add_custom_button(
			__("Upgrade Thumbnail Quality"),
			() => {
				frappe.call({
					method: "prism.api.moodboard_v2.regenerate_moodboard_thumbnail",
					args: { id: frm.doc.name },
					freeze: true,
					freeze_message: __("Upgrading thumbnail quality…"),
					callback: (r) => {
						const m = r.message || {};
						if (m.success) {
							frappe.show_alert({ message: __("Thumbnail upgraded"), indicator: "green" });
							frm.reload_doc();
						} else {
							frappe.msgprint({
								title: __("Could not upgrade thumbnail"),
								indicator: "red",
								message: m.error || __("No source image found."),
							});
						}
					},
				});
			},
			__("Media")
		);

		// --- Upgrade EVERY board's thumbnail to high quality (background) ---
		if (frappe.user.has_role("System Manager")) {
			frm.add_custom_button(
				__("Upgrade ALL Thumbnails (background)"),
				() => {
					frappe.confirm(
						__(
							"Upgrade the thumbnail to high quality for EVERY active board " +
								"(Draft + Published)? Runs in the background; does not touch the full-size images."
						),
						() => {
							frappe.call({
								method: "prism.api.moodboard_v2.regenerate_all_thumbnails",
								args: { enqueue: 1 },
								freeze: true,
								callback: () => {
									frappe.msgprint({
										title: __("Thumbnail upgrade started"),
										indicator: "blue",
										message: __(
											"Upgrading all thumbnails in the background. " +
												"Watch Error Log (filter method like %regenerate_all_thumbnails%) for failures."
										),
									});
								},
							});
						}
					);
				},
				__("Media")
			);
		}
	},
});
