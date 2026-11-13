//! 构建专用的当前工作树安装入口，不加载 APP、不检出或拉取 Git。

use crate::app::App;
use crate::nono_build;
use crate::nono_portable::{read_manifest, verify_working};
use crate::nono_state::{
    canonical_root, exclusive_lease, file_stamp, path_under, read_object, write_json,
};
use crate::utils::error::Error;
use anyhow::{anyhow, ensure, Result};
use serde_json::{json, Value};
use std::path::Path;

pub(crate) fn build_info() -> Value {
    json!({
        "app_name": "NoNo", "version": "1.2.7",
        "source_sha": nono_build::SOURCE_SHA, "action_sha": nono_build::ACTION_SHA,
        "adaptation_sha256": nono_build::ADAPTATION_SHA256,
        "source_tree_sha256": nono_build::SOURCE_TREE_SHA256,
    })
}

pub(crate) async fn nono_install_working(
    profile_name: &str,
    manifest_path: &Path,
) -> Result<App, Error> {
    let root = canonical_root(&std::env::current_dir()?)?;
    let _receive = exclusive_lease(&root, "receive.lock")?;
    let _idle = exclusive_lease(&root, "runtime.lock")?;
    let path = if manifest_path.is_absolute() {
        manifest_path.to_path_buf()
    } else {
        root.join(manifest_path)
    };
    let path = path
        .canonicalize()
        .map_err(|_| anyhow!("无法读取安装清单（E_INSTALL_MANIFEST）"))?;
    if !path.starts_with(&root) {
        return Err(anyhow!("安装清单超出构建目录（E_INSTALL_PATH）").into());
    }
    let manifest = read_manifest(&path)?;
    if manifest.build_kind != "working_install" {
        return Err(anyhow!("当前工作树安装清单类型无效（E_INSTALL_KIND）").into());
    }
    verify_working(&root, &manifest)?;
    let record_path = path_under(&root, "data/state/NoNo/install-working.json")?;
    let digest = file_stamp(&path)?.sha256;
    write_json(
        &record_path,
        &json!({
            "schema_version": 1, "state": "pending", "bundle_id": manifest.bundle_id,
            "manifest_sha256": digest, "source_sha": manifest.source_sha,
        }),
    )?;
    let result: Result<App> = async {
        let raw = read_object(&path_under(&root, "data/apps/NoNo/app.json")?)?
            .ok_or_else(|| anyhow!("当前工作树安装缺少正式底座状态（E_INSTALL_APP）"))?;
        let mut app: App = serde_json::from_value(Value::Object(raw))
            .map_err(|_| anyhow!("正式底座应用状态无效（E_INSTALL_APP）"))?;
        ensure!(app.name == "NoNo", "正式底座应用名无效（E_INSTALL_APP）");
        app.current_version = Some(manifest.base_update_version.clone());
        app.current_version_missing = false;
        let marker = path_under(&root, "data/apps/NoNo/working/.pip_update_needed.tmp")?;
        ensure!(
            !marker.exists(),
            "当前工作树存在失败标记（E_INSTALL_MARKER）"
        );
        let installed = crate::app_service::install_app_from_working(app, profile_name).await?;
        verify_working(&root, &manifest)?;
        ensure!(!marker.exists(), "依赖安装未完成（E_INSTALL_MARKER）");
        write_json(
            &record_path,
            &json!({
                "schema_version": 1, "state": "completed", "bundle_id": manifest.bundle_id,
                "manifest_sha256": digest, "source_sha": manifest.source_sha,
                "actual_profile": installed.current_profile,
            }),
        )?;
        Ok(installed)
    }
    .await;
    match result {
        Ok(app) => Ok(app),
        Err(error) => {
            // 失败记录只含固定阶段和身份，不输出依赖参数、配置或账号。
            let _ = write_json(
                &record_path,
                &json!({
                    "schema_version": 1, "state": "failed", "bundle_id": manifest.bundle_id,
                    "manifest_sha256": digest, "error_code": "E_INSTALL_WORKING",
                }),
            );
            Err(error.into())
        }
    }
}
