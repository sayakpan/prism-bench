// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.ui.form.on("Page Builder", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (!frappe.user.has_role("System Manager")) return;

		// --- Move this page's local /files images to S3 and reclaim disk ---
		frm.add_custom_button(__("Migrate Images to S3"), () => {
			frappe.confirm(
				__(
					"Move every local image on this page ({0}) to S3 — including all " +
						"version-history snapshots — and delete the local copies to reclaim disk? " +
						"Restore still works (snapshots are rewritten to S3). " +
						"Images already on S3 are skipped. This cannot be undone.",
					[frm.doc.brand_id || frm.doc.name]
				),
				() => {
					frappe.call({
						method: "prism.api.page_builder.migrate_images_to_s3",
						args: { name: frm.doc.name },
						freeze: true,
						freeze_message: __("Uploading images to S3…"),
						callback: (r) => {
							const m = r.message || {};
							if (!m.success) {
								frappe.msgprint({
									title: __("Migration failed"),
									indicator: "red",
									message: m.error || __("Could not migrate images."),
								});
								return;
							}

							const s = m.data || {};
							if (!s.migrated && !s.failed && !s.missing) {
								frappe.show_alert({
									message: __("No local images to migrate — already on S3."),
									indicator: "blue",
								});
								return;
							}

							let msg = __("Migrated {0} image(s) to S3", [s.migrated || 0]);
							msg += ", " + __("rewrote {0} snapshot(s)", [s.snapshots || 0]);
							msg += ", " + __("deleted {0} local file(s).", [s.deleted || 0]);
							if (s.missing) msg += " " + __("{0} dangling ref(s) skipped.", [s.missing]);
							if (s.failed) msg += " " + __("{0} failed (kept local; see Error Log).", [s.failed]);

							frappe.msgprint({
								title: __("Image migration complete"),
								indicator: s.failed ? "orange" : "green",
								message: msg,
							});
							frm.reload_doc();
						},
					});
				}
			);
		});
	},
});
