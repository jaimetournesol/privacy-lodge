//! System tray: a status line (disabled item, updated as phase changes) plus
//! Open / Pause / Quit. Quit goes through Supervisor::shutdown so sidecars
//! get a graceful SIGTERM before the process exits.

use std::sync::Mutex;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, Wry};

use crate::supervisor::{self, Supervisor};

pub struct TrayHandles {
    status_item: Mutex<Option<MenuItem<Wry>>>,
}

pub fn init(app: &AppHandle) -> tauri::Result<()> {
    let status = MenuItem::with_id(app, "status", "Privacy Lodge", false, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "Open Privacy Lodge", true, None::<&str>)?;
    let pause =
        MenuItem::with_id(app, "pause", "Pause box (people can't reach you)", true, None::<&str>)?;
    let quit =
        MenuItem::with_id(app, "quit", "Quit (your box goes offline)", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&status, &open, &pause, &quit])?;

    let mut builder = TrayIconBuilder::with_id("main")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app: &AppHandle, event| match event.id.as_ref() {
            "open" => {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.unminimize();
                    let _ = window.set_focus();
                }
            }
            "pause" => supervisor::stop_lifecycle(app),
            "quit" => {
                app.state::<Supervisor>().shutdown();
                app.exit(0);
            }
            _ => {}
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;

    app.manage(TrayHandles { status_item: Mutex::new(Some(status)) });
    Ok(())
}

/// Update the disabled status line. No-op before the tray exists (early
/// state mutations during setup() ordering).
pub fn set_status_text(app: &AppHandle, text: &str) {
    if let Some(handles) = app.try_state::<TrayHandles>() {
        if let Some(item) = handles.status_item.lock().unwrap().as_ref() {
            let _ = item.set_text(text);
        }
    }
}
