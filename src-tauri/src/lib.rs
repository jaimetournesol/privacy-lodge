//! Privacy Lodge desktop backend. The frontend polls get_status() every 1.5s;
//! nothing is pushed via events.

mod account;
mod agent;
mod backup;
mod commands;
mod config;
pub mod crypto;
mod envcompat; // pub: pl-crypt (backup-bundle encryption CLI) runs this exact code
mod fedauth;
mod pairing;
mod setup_server;
mod state;
mod supervisor;
mod tray;
mod updater;
mod words;

use tauri::Manager;
use tauri_plugin_opener::OpenerExt;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(state::AppState::default())
        .manage(supervisor::Supervisor::default())
        .invoke_handler(tauri::generate_handler![
            commands::get_status,
            commands::suggest_password,
            commands::begin_setup,
            commands::get_recovery_kit,
            commands::confirm_recovery_word,
            commands::save_recovery_kit_html,
            commands::get_connect_qr,
            commands::stop_box,
            commands::start_box,
            commands::detect_legacy_install,
            commands::get_join_info,
            commands::app_info,
            commands::reset_box,
            commands::pair_create,
            commands::pair_accept,
            commands::pair_list,
            commands::pair_remove,
            commands::get_setup_url,
            commands::open_setup_page,
        ])
        .setup(|app| {
            state::load_persisted(app.handle());
            tray::init(app.handle())?;
            // First-run vs resume (appliance-UX feature A).
            //  - Provisioned box (has an onion): resume it. Opt-in via AUTOSTART for the
            //    multi-box launcher; GUI default (come up Stopped, user clicks Start) is
            //    unchanged when AUTOSTART is unset.
            //  - Fresh box + env creds (headless/tests/testbed): drive begin_setup from
            //    env, no web server — the existing demo/testbed path, unchanged.
            //  - Fresh box, no creds: serve the one-page web setup. The GUI opens it in
            //    the default browser; Docker (AUTOSTART, no creds) prints the URL from
            //    the entrypoint. Loopback-only; shuts itself down once the phone signs in.
            let autostart = crate::envcompat::var("AUTOSTART").ok().as_deref() == Some("1");
            if state::read(app.handle(), |i| i.onion.is_some()) {
                if autostart {
                    supervisor::start_lifecycle(app.handle(), None);
                }
            } else if let (Ok(user), Ok(pass)) = (
                crate::envcompat::var("PROVISION_USER"),
                crate::envcompat::var("PROVISION_PASS"),
            ) {
                let box_name = crate::envcompat::var("PROVISION_BOX")
                    .unwrap_or_else(|_| format!("{user}box"));
                if let Err(e) = commands::begin_setup(app.handle().clone(), box_name, user, pass) {
                    eprintln!("[privacy-lodge] headless provision failed: {e}");
                }
            } else {
                setup_server::start(app.handle().clone());
                if !autostart {
                    let url = setup_server::setup_url();
                    if let Err(e) = app.handle().opener().open_url(url, None::<&str>) {
                        eprintln!("[privacy-lodge] couldn't open the setup page: {e}");
                    }
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                // Graceful SIGTERM to sidecars; kill_on_drop(true) on each
                // child is the SIGKILL backstop when the runtime tears down.
                app.state::<supervisor::Supervisor>().shutdown();
            }
        });
}
