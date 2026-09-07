// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.ui.form.on("Moodboard Style", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;

		// --- Migrate THIS style's image to S3 ---
		frm.add_custom_button(
			__("Migrate image to S3"),
			() => {
				frappe.call({
					method: "prism.api.moodboard_style.migrate_style_images",
					args: { ids: [frm.doc.name] },
					freeze: true,
					freeze_message: __("Migrating image to S3…"),
					callback: (r) => {
						const m = r.message || {};
						frappe.msgprint({
							title: __("Migration complete"),
							indicator: m.migrated ? "green" : "blue",
							message: __("Migrated: {0}, skipped (already S3): {1}, failed: {2}", [
								m.migrated || 0,
								m.skipped || 0,
								m.failed || 0,
							]),
						});
						frm.reload_doc();
					},
				});
			},
			__("Media")
		);

		// --- Migrate EVERY style's image in the background ---
		frm.add_custom_button(
			__("Migrate ALL style images (background)"),
			() => {
				frappe.confirm(
					__(
						"Migrate the per-garment image of EVERY style still on local storage to S3? " +
							"Runs in the background and does not change published/unpublished status."
					),
					() => {
						frappe.call({
							method: "prism.api.moodboard_style.migrate_style_images",
							args: { enqueue: 1 },
							freeze: true,
							callback: () => {
								frappe.msgprint({
									title: __("Migration started"),
									indicator: "blue",
									message: __(
										"Migrating all style images in the background. " +
											"Watch Error Log (filter method like %migrate_style_images%) for failures."
									),
								});
							},
						});
					}
				);
			},
			__("Media")
		);
	},
});
