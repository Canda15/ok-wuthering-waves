//! 启动前接收完整便携环境；任何失败都阻止官方 App 加载和旧库迁移。

use crate::app::{
    App, AppUpdateState, UPDATE_METHOD_OPTION_AUTO, UPDATE_METHOD_OPTION_AUTO_PRE_RELEASE,
    UPDATE_METHOD_OPTION_MANUAL,
};
use crate::nono_build;
use crate::nono_state::{
    canonical_json, canonical_root, collect_tree, copy_tree, copy_verified, digest_bytes,
    ensure_directory, exclusive_lease, file_stamp, merge_app_object, path_under, read_object,
    relative_path, rename_no_replace, write_json, FileStamp, TreeStamp,
};
use anyhow::{anyhow, bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use tracing::{info, warn};

const APP_ROOT: &str = "data/apps/NoNo";
const INCOMING_ROOT: &str = "data/incoming/NoNo";
const STATE_ROOT: &str = "data/state/NoNo";
const USER_ROOT: &str = "data/user/NoNo";
pub(crate) const OFFICIAL_ORIGIN: &str = "https://github.com/Canda15/nono-update.git";
static BOOTSTRAP_UPDATES_BLOCKED: AtomicBool = AtomicBool::new(false);
#[cfg(test)]
thread_local! {
    static TEST_INTERRUPTION: std::cell::RefCell<Option<String>> = const { std::cell::RefCell::new(None) };
}

fn checkpoint(name: &str) -> Result<()> {
    #[cfg(test)]
    if TEST_INTERRUPTION.with(|point| {
        let mut point = point.borrow_mut();
        if point.as_deref() == Some(name) {
            point.take();
            true
        } else {
            false
        }
    }) {
        bail!("合成中断（E_TEST_INTERRUPTION）");
    }
    let _ = name;
    Ok(())
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LauncherIdentity {
    pub version: String,
    pub source_sha: String,
    pub action_sha: String,
    pub adaptation_sha256: String,
    pub source_tree_sha256: String,
    pub exe_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct BundleManifest {
    pub schema_version: u32,
    pub bundle_id: String,
    pub app_name: String,
    pub build_kind: String,
    pub source_sha: String,
    pub base_update_version: String,
    pub base_update_commit: String,
    pub origin: String,
    pub requirements_sha256: String,
    pub app_seed_file: String,
    pub launcher: LauncherIdentity,
    pub working_files: BTreeMap<String, FileStamp>,
    pub payload_files: BTreeMap<String, FileStamp>,
    pub payload_directories: BTreeSet<String>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct SelectedPackage {
    schema_version: u32,
    bundle_id: String,
    manifest_sha256: String,
}

#[derive(Clone, Deserialize, Serialize)]
struct AcceptedReceipt {
    schema_version: u32,
    bundle_id: String,
    manifest_sha256: String,
    transaction_id: String,
    source_sha: String,
    base_update_version: String,
    base_update_commit: String,
    launcher: LauncherIdentity,
    bootstrap_pending: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum ReceiveStage {
    Verified,
    OriginalsProtected,
    Protected,
    Prepared,
    Swapping,
    Committing,
    Accepted,
    CleanupPending,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", content = "stamp", rename_all = "snake_case")]
enum PathStamp {
    File(FileStamp),
    Directory(TreeStamp),
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct SwapOperation {
    live: String,
    prepared: Option<String>,
    backup: String,
    original: Option<PathStamp>,
    replacement: Option<PathStamp>,
    intent: String,
}

#[derive(Clone, Deserialize, Serialize)]
struct ReceiveJournal {
    schema_version: u32,
    transaction_id: String,
    manifest_sha256: String,
    manifest: BundleManifest,
    stage: ReceiveStage,
    originals: BTreeMap<String, PathStamp>,
    operations: Vec<SwapOperation>,
}

#[derive(Debug)]
pub(crate) enum ReceiveOutcome {
    NoPackage,
    AlreadyAccepted,
    Accepted,
}

fn sha(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

fn state_path(root: &Path, name: &str) -> Result<PathBuf> {
    path_under(root, &format!("{STATE_ROOT}/{name}"))
}

fn parse_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    let object = read_object(path)?.ok_or_else(|| anyhow!("缺少事务文件（E_STATE_MISSING）"))?;
    serde_json::from_value(Value::Object(object))
        .map_err(|_| anyhow!("事务文件格式无效（E_STATE_FORMAT）"))
}

pub(crate) fn read_manifest(path: &Path) -> Result<BundleManifest> {
    let manifest: BundleManifest = parse_json(path)?;
    ensure!(
        manifest.schema_version == 1 && manifest.app_name == "NoNo",
        "载荷协议不匹配（E_BUNDLE_SCHEMA）"
    );
    ensure!(
        sha(&manifest.bundle_id, 64)
            && sha(&manifest.source_sha, 40)
            && sha(&manifest.base_update_commit, 40),
        "载荷身份无效（E_BUNDLE_ID）"
    );
    ensure!(
        crate::git::is_version_tag(&manifest.base_update_version)
            && (manifest.build_kind == "release_tag"
                || regex::Regex::new(r"^v?\d+\.\d+\.\d+$")?
                    .is_match(&manifest.base_update_version)),
        "更新锚点与构建模式不匹配（E_BUNDLE_VERSION）"
    );
    ensure!(
        manifest.origin == OFFICIAL_ORIGIN && manifest.app_seed_file == "app-seed.json",
        "载荷来源不匹配（E_BUNDLE_ORIGIN）"
    );
    ensure!(
        matches!(
            manifest.build_kind.as_str(),
            "commit_snapshot" | "release_tag" | "working_install"
        ),
        "载荷模式无效（E_BUNDLE_KIND）"
    );
    let mut identity = serde_json::to_value(&manifest)?;
    identity
        .as_object_mut()
        .ok_or_else(|| anyhow!("载荷身份无效（E_BUNDLE_ID）"))?
        .remove("bundle_id");
    ensure!(
        digest_bytes(&canonical_json(&identity)?) == manifest.bundle_id,
        "载荷身份摘要不匹配（E_BUNDLE_ID）"
    );
    let launcher = &manifest.launcher;
    ensure!(
        launcher.version == "1.2.7"
            && launcher.source_sha == nono_build::SOURCE_SHA
            && launcher.action_sha == nono_build::ACTION_SHA
            && launcher.adaptation_sha256 == nono_build::ADAPTATION_SHA256
            && launcher.source_tree_sha256 == nono_build::SOURCE_TREE_SHA256
            && sha(&launcher.exe_sha256, 64),
        "启动器与载荷不配套（E_LAUNCHER_ID）"
    );
    for (name, stamp) in manifest
        .working_files
        .iter()
        .chain(manifest.payload_files.iter())
    {
        relative_path(name)?;
        ensure!(sha(&stamp.sha256, 64), "文件摘要无效（E_BUNDLE_DIGEST）");
    }
    for name in &manifest.payload_directories {
        relative_path(name)?;
    }
    validate_payload_boundaries(&manifest)?;
    Ok(manifest)
}

fn validate_payload_boundaries(manifest: &BundleManifest) -> Result<()> {
    ensure!(
        manifest.payload_files.keys().all(|name| {
            name.starts_with("repo/")
                || name.starts_with("working/")
                || name.starts_with("python/")
                || matches!(
                    name.as_str(),
                    "app-seed.json" | "root/pyappify.yml" | "root/build-info.json"
                )
        }),
        "载荷包含未授权路径（E_PAYLOAD_BOUNDARY）"
    );
    ensure!(
        manifest.payload_directories.iter().all(|name| {
            (matches!(name.as_str(), "repo" | "working" | "python" | "root")
                || name.starts_with("repo/")
                || name.starts_with("working/")
                || name.starts_with("python/"))
                && !manifest.payload_files.contains_key(name)
        }),
        "载荷目录包含未授权路径（E_PAYLOAD_BOUNDARY）"
    );
    // 空目录同样属于交付边界；Windows 大小写别名不能绕过用户数据和失败标记保护。
    ensure!(
        !manifest
            .payload_files
            .keys()
            .chain(manifest.payload_directories.iter())
            .any(|name| (name.starts_with("working/")
                && name.split('/').nth(1).is_some_and(|part| matches!(
                    part.to_ascii_lowercase().as_str(),
                    "configs" | "ok_tasks" | "ok_import" | "ok_templates" | "screenshots" | "logs"
                )))
                || name
                    .to_ascii_lowercase()
                    .ends_with(".pip_update_needed.tmp")),
        "载荷包含用户数据或失败标记（E_PAYLOAD_PRIVATE）"
    );
    Ok(())
}

pub(crate) fn strict_working_app(working: &Path) -> Result<App> {
    crate::app::read_app_from_yml_strict(&working.join("pyappify.yml"))
}

pub(crate) fn verify_working(root: &Path, manifest: &BundleManifest) -> Result<App> {
    let working = path_under(root, &format!("{APP_ROOT}/working"))?;
    ensure!(
        collect_tree(&working)?.files == manifest.working_files,
        "当前工作树发生变化（E_WORKING_DIGEST）"
    );
    ensure!(
        file_stamp(&working.join("requirements.txt"))?.sha256 == manifest.requirements_sha256,
        "当前依赖摘要不匹配（E_REQUIREMENTS_DIGEST）"
    );
    let app = strict_working_app(&working)?;
    ensure!(
        app.name == "NoNo" && !app.profiles.is_empty(),
        "当前应用配置无效（E_APP_CONFIG）"
    );
    for profile in &app.profiles {
        ensure!(
            profile.git_url == manifest.origin
                && profile.main_script == "selfhost/NoNo.exe"
                && profile.requirements == "requirements.txt"
                && profile.requires_python == "3.12",
            "当前运行入口或依赖无效（E_APP_PROFILE）"
        );
    }
    verify_git_anchor(&path_under(root, &format!("{APP_ROOT}/repo"))?, manifest)?;
    Ok(app)
}

pub(crate) fn verify_git_anchor(repo: &Path, manifest: &BundleManifest) -> Result<()> {
    ensure!(
        !repo.join(".git").exists(),
        "载荷包含旧仓库布局（E_GIT_LAYOUT）"
    );
    let repository =
        git2::Repository::open_bare(repo).map_err(|_| anyhow!("载荷不是裸仓库（E_GIT_LAYOUT）"))?;
    ensure!(repository.is_bare(), "载荷不是裸仓库（E_GIT_LAYOUT）");
    let head = repository.head()?.peel_to_commit()?.id().to_string();
    let tag = repository
        .find_reference(&format!("refs/tags/{}", manifest.base_update_version))?
        .peel_to_commit()?
        .id()
        .to_string();
    let remote = repository.find_remote("origin")?;
    ensure!(
        head == manifest.base_update_commit
            && tag == head
            && remote.url()? == manifest.origin.as_str(),
        "正式更新锚点发生变化（E_GIT_ANCHOR）"
    );
    Ok(())
}

fn payload_tree(manifest: &BundleManifest) -> TreeStamp {
    TreeStamp {
        files: manifest.payload_files.clone(),
        directories: manifest.payload_directories.clone(),
    }
}

fn verify_payload(
    root: &Path,
    payload: &Path,
    manifest: &BundleManifest,
    verify_launcher: bool,
) -> Result<()> {
    validate_payload_boundaries(manifest)?;
    let mut actual = collect_tree(payload)?;
    actual.files.remove("manifest.json");
    let internally_prepared = payload.starts_with(root.join(format!("{STATE_ROOT}/prepared")));
    if internally_prepared {
        actual.files.remove("app.json");
    }
    ensure!(
        actual == payload_tree(manifest),
        "载荷缺项或含额外文件（E_PAYLOAD_FILES）"
    );
    let required = [
        "repo/HEAD",
        "repo/config",
        "working/main.py",
        "working/config.py",
        "working/requirements.txt",
        "working/pyappify.yml",
        "working/selfhost/NoNo.exe",
        "working/selfhost/ok_pin.json",
        "python/python.exe",
        "python/python3.dll",
        "python/python312.dll",
        "app-seed.json",
        "root/pyappify.yml",
        "root/build-info.json",
    ];
    ensure!(
        required
            .iter()
            .all(|name| manifest.payload_files.contains_key(*name)),
        "载荷缺少完整运行环境（E_PAYLOAD_REQUIRED）"
    );
    ensure!(
        !verify_launcher
            || file_stamp(&root.join("NoNo.exe"))?.sha256 == manifest.launcher.exe_sha256,
        "根启动器与载荷不配套（E_LAUNCHER_EXE）"
    );
    ensure!(
        collect_tree(&payload.join("working"))?.files == manifest.working_files,
        "载荷工作树不匹配（E_WORKING_DIGEST）"
    );
    ensure!(
        file_stamp(&payload.join("working/requirements.txt"))?.sha256
            == manifest.requirements_sha256,
        "载荷依赖摘要不匹配（E_REQUIREMENTS_DIGEST）"
    );
    let app = strict_working_app(&payload.join("working"))?;
    let root_app = crate::app::read_app_from_yml_strict(&payload.join("root/pyappify.yml"))?;
    ensure!(
        app.name == "NoNo" && root_app.name == "NoNo" && app.profiles == root_app.profiles,
        "根配置与当前工作树不匹配（E_APP_CONFIG）"
    );
    for profile in &app.profiles {
        ensure!(
            profile.main_script == "selfhost/NoNo.exe" && profile.git_url == manifest.origin,
            "载荷运行入口无效（E_APP_PROFILE）"
        );
    }
    let seed = read_object(&payload.join(&manifest.app_seed_file))?
        .ok_or_else(|| anyhow!("载荷缺少安装种子（E_APP_SEED）"))?;
    let seed: App = serde_json::from_value(Value::Object(seed))
        .map_err(|_| anyhow!("安装种子格式无效（E_APP_SEED）"))?;
    ensure!(
        seed.name == "NoNo"
            && seed.installed
            && seed.current_version.as_deref() == Some(manifest.base_update_version.as_str())
            && seed.profiles == app.profiles,
        "安装种子与载荷不匹配（E_APP_SEED）"
    );
    verify_git_anchor(&payload.join("repo"), manifest)
}

fn stamp_path(path: &Path) -> Result<Option<PathStamp>> {
    if !path.exists() {
        return Ok(None);
    }
    if path.is_dir() {
        Ok(Some(PathStamp::Directory(collect_tree(path)?)))
    } else {
        Ok(Some(PathStamp::File(file_stamp(path)?)))
    }
}

fn copy_path(root: &Path, source: &Path, target: &Path, stamp: &PathStamp) -> Result<()> {
    match stamp {
        PathStamp::File(stamp) => copy_verified(root, source, target, stamp),
        PathStamp::Directory(stamp) => copy_tree(root, source, target, stamp),
    }
}

fn journal_path(root: &Path) -> Result<PathBuf> {
    state_path(root, "receive.json")
}
fn save_journal(root: &Path, journal: &ReceiveJournal) -> Result<()> {
    write_json(&journal_path(root)?, journal)
}
fn prepared_root(root: &Path, journal: &ReceiveJournal) -> Result<PathBuf> {
    path_under(
        root,
        &format!("{STATE_ROOT}/prepared/{}", journal.transaction_id),
    )
}
fn backup_root(root: &Path, journal: &ReceiveJournal) -> Result<PathBuf> {
    path_under(
        root,
        &format!("data/backups/NoNo/{}", journal.transaction_id),
    )
}
fn original_path(root: &Path, journal: &ReceiveJournal, relative: &str) -> Result<PathBuf> {
    Ok(backup_root(root, journal)?
        .join("original")
        .join(relative_path(relative)?))
}

fn assert_no_running_process(root: &Path) -> Result<()> {
    // 旧宿主没有租约，用实际进程镜像补齐从 launcher-1 升级的占用检查。
    let system = sysinfo::System::new_all();
    let own_pid = std::process::id();
    for (pid, process) in system.processes() {
        if pid.as_u32() == own_pid {
            continue;
        }
        if let Some(image) = process.exe() {
            if image
                .canonicalize()
                .ok()
                .is_some_and(|path| path.starts_with(root))
            {
                bail!("此目录仍有程序运行，请退出后重试（E_PROCESS_BUSY）");
            }
        }
    }
    Ok(())
}

fn new_journal(
    root: &Path,
    manifest: BundleManifest,
    manifest_sha256: String,
) -> Result<ReceiveJournal> {
    let sequence = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    let transaction_id = format!("{}-{sequence}", &manifest.bundle_id[..16]);
    let mut originals = BTreeMap::new();
    for relative in [
        APP_ROOT,
        "data/config",
        USER_ROOT,
        "pyappify.yml",
        "build-info.json",
    ] {
        if let Some(stamp) = stamp_path(&path_under(root, relative)?)? {
            originals.insert(relative.to_string(), stamp);
        }
    }
    Ok(ReceiveJournal {
        schema_version: 1,
        transaction_id,
        manifest_sha256,
        manifest,
        stage: ReceiveStage::Verified,
        originals,
        operations: Vec::new(),
    })
}

fn protect_originals(root: &Path, journal: &mut ReceiveJournal) -> Result<()> {
    for (relative, stamp) in &journal.originals {
        let source = path_under(root, relative)?;
        ensure!(
            stamp_path(&source)?.as_ref() == Some(stamp),
            "保护期间原安装发生变化（E_ORIGINAL_CHANGED）"
        );
        copy_path(
            root,
            &source,
            &original_path(root, journal, relative)?,
            stamp,
        )?;
        checkpoint("original_copied")?;
    }
    for (relative, stamp) in &journal.originals {
        ensure!(
            stamp_path(&path_under(root, relative)?)?.as_ref() == Some(stamp),
            "保护期间原安装发生变化（E_ORIGINAL_CHANGED）"
        );
    }
    // 先形成不可覆盖原件，再严格解析；坏 JSON 不会被默认配置覆盖。
    for relative in [
        format!("{APP_ROOT}/app.json"),
        "data/config/app_config.json".to_string(),
    ] {
        let original = read_object(&original_path(root, journal, &relative)?)?;
        if relative.ends_with("/app.json") {
            if let Some(original) = original {
                serde_json::from_value::<App>(Value::Object(original))
                    .map_err(|_| anyhow!("旧应用状态格式无效，原件已保留（E_APP_ORIGINAL）"))?;
            }
        }
    }
    journal.stage = ReceiveStage::OriginalsProtected;
    save_journal(root, journal)?;
    checkpoint("originals_protected")
}

fn migrate_user_data(root: &Path, journal: &mut ReceiveJournal) -> Result<()> {
    // 新安装无原件或从保护阶段恢复时，本阶段仍要创建保护记录的受检父目录。
    ensure_directory(
        root,
        &format!("data/backups/NoNo/{}", journal.transaction_id),
    )?;
    ensure_directory(root, USER_ROOT)?;
    let supported = [
        "configs",
        "ok_tasks",
        "ok_import",
        "ok_templates",
        "screenshots",
        "logs",
        ".ok_cache",
    ];
    for source_kind in ["working", "repo"] {
        let source = original_path(root, journal, &format!("{APP_ROOT}/{source_kind}"))?;
        if !source.is_dir() {
            continue;
        }
        let tree = collect_tree(&source)?;
        for directory in &tree.directories {
            let first = directory.split('/').next().unwrap_or_default();
            if let Some(canonical) = supported
                .iter()
                .find(|name| name.eq_ignore_ascii_case(first))
            {
                // 只统一已知目录首段；旧文件及其子目录原名继续保留，避免账号退回默认值。
                let normalized = format!("{canonical}{}", &directory[first.len()..]);
                ensure_directory(root, &format!("{USER_ROOT}/{normalized}"))?;
            }
        }
        for (relative, stamp) in &tree.files {
            let first = relative.split('/').next().unwrap_or_default();
            if matches!(
                first,
                ".git" | "HEAD" | "config" | "objects" | "refs" | "hooks" | "logs"
            ) && source_kind == "repo"
            {
                continue;
            }
            let source_file = source.join(relative_path(relative)?);
            if let Some(canonical) = supported
                .iter()
                .find(|name| name.eq_ignore_ascii_case(first))
            {
                if *canonical == "configs"
                    && relative.split('/').count() == 2
                    && source_file.extension().is_some_and(|extension| {
                        extension.to_string_lossy().eq_ignore_ascii_case("json")
                    })
                {
                    read_object(&source_file)?;
                }
                let normalized = format!("{canonical}{}", &relative[first.len()..]);
                let target = path_under(root, &format!("{USER_ROOT}/{normalized}"))?;
                if !target.exists() || file_stamp(&target)? == *stamp {
                    copy_verified(root, &source_file, &target, stamp)?;
                } else {
                    let conflict = path_under(
                        root,
                        &format!(
                            "{USER_ROOT}/conflicts/{}/{source_kind}/{relative}",
                            journal.transaction_id
                        ),
                    )?;
                    copy_verified(root, &source_file, &conflict, stamp)?;
                }
            } else if first != ".git" {
                // 未知附件和用户改写源码留在独立目录，绝不遮盖新 working。
                let extra = path_under(
                    root,
                    &format!(
                        "{USER_ROOT}/extras/{}/{source_kind}/{relative}",
                        journal.transaction_id
                    ),
                )?;
                copy_verified(root, &source_file, &extra, stamp)?;
            }
        }
    }
    let protection = serde_json::json!({
        "schema_version": 1, "transaction_id": journal.transaction_id,
        "bundle_id": journal.manifest.bundle_id, "complete": true,
        "originals": journal.originals,
    });
    write_json(
        &backup_root(root, journal)?.join("protection.json"),
        &protection,
    )?;
    journal.stage = ReceiveStage::Protected;
    save_journal(root, journal)?;
    checkpoint("protected")
}

fn preference<'a>(
    old: &'a Map<String, Value>,
    global: &'a Map<String, Value>,
    key: &str,
    legacy: &str,
) -> Option<&'a Value> {
    old.get(key).or_else(|| global.get(legacy))
}

fn prepare_app(root: &Path, journal: &ReceiveJournal, prepared: &Path) -> Result<()> {
    let seed = read_object(&prepared.join("app-seed.json"))?
        .ok_or_else(|| anyhow!("缺少完整安装种子（E_APP_SEED）"))?;
    let old = read_object(&original_path(
        root,
        journal,
        &format!("{APP_ROOT}/app.json"),
    )?)?
    .unwrap_or_default();
    let global = read_object(&original_path(
        root,
        journal,
        "data/config/app_config.json",
    )?)?
    .unwrap_or_default();
    let mut app: App = serde_json::from_value(Value::Object(seed.clone()))
        .map_err(|_| anyhow!("安装种子格式无效（E_APP_SEED）"))?;
    if let Some(value) = preference(&old, &global, "auto_start", "Auto Start") {
        app.auto_start = value
            .as_bool()
            .ok_or_else(|| anyhow!("启动偏好必须为布尔值（E_APP_PREFERENCE）"))?;
    } else {
        app.auto_start = false;
    }
    if let Some(value) = preference(&old, &global, "update_method", "Update Method") {
        let value = value
            .as_str()
            .ok_or_else(|| anyhow!("更新偏好类型无效（E_APP_PREFERENCE）"))?;
        ensure!(
            matches!(
                value,
                UPDATE_METHOD_OPTION_MANUAL
                    | UPDATE_METHOD_OPTION_AUTO
                    | UPDATE_METHOD_OPTION_AUTO_PRE_RELEASE
            ),
            "更新偏好无效，原件已保留（E_APP_PREFERENCE）"
        );
        app.update_method = value.to_string();
    }
    if let Some(profile) = old.get("current_profile").and_then(Value::as_str) {
        if app.profiles.iter().any(|item| item.name == profile) {
            app.current_profile = profile.to_string();
        }
    }
    if !app
        .profiles
        .iter()
        .any(|item| item.name == app.current_profile)
    {
        app.current_profile = app
            .profiles
            .first()
            .ok_or_else(|| anyhow!("缺少运行配置（E_APP_PROFILE）"))?
            .name
            .clone();
    }
    if let Some(last_start) = old.get("last_start") {
        app.last_start = serde_json::from_value(last_start.clone())
            .map_err(|_| anyhow!("启动状态格式无效（E_APP_PREFERENCE）"))?;
    }
    if let Some(show_defender) = old.get("show_add_defender") {
        app.show_add_defender = show_defender
            .as_bool()
            .ok_or_else(|| anyhow!("启动状态必须为布尔值（E_APP_PREFERENCE）"))?;
    }
    app.running = false;
    app.app_starting_version = None;
    app.update_state = AppUpdateState::Idle;
    app.update_target_version = None;
    app.update_error = None;
    app.update_note.clear();
    app.installed = true;
    app.current_version = Some(journal.manifest.base_update_version.clone());
    app.current_version_missing = false;
    let mut merged = seed;
    merged.extend(old);
    let merged = merge_app_object(merged, serde_json::to_value(&app)?)?;
    let candidate = prepared.join("app.json");
    if let Some(existing) = read_object(&candidate)? {
        ensure!(
            existing == merged,
            "暂存安装状态发生变化（E_PREPARED_APP_CHANGED）"
        );
        Ok(())
    } else {
        write_json(&candidate, &merged)
    }
}

fn prepare_package(root: &Path, journal: &mut ReceiveJournal) -> Result<()> {
    let incoming = path_under(
        root,
        &format!("{INCOMING_ROOT}/{}", journal.manifest.bundle_id),
    )?;
    // 已核验旧包后，新 selected 可能更换根 EXE；恢复旧事务只校验保存的载荷身份。
    verify_payload(root, &incoming, &journal.manifest, false)?;
    let prepared = prepared_root(root, journal)?;
    copy_tree(root, &incoming, &prepared, &{
        let mut stamp = payload_tree(&journal.manifest);
        stamp.files.insert(
            "manifest.json".to_string(),
            file_stamp(&incoming.join("manifest.json"))?,
        );
        if prepared.join("app.json").exists() {
            stamp.files.insert(
                "app.json".to_string(),
                file_stamp(&prepared.join("app.json"))?,
            );
        }
        stamp
    })?;
    verify_payload(root, &prepared, &journal.manifest, false)?;
    prepare_app(root, journal, &prepared)?;
    checkpoint("candidate_ready")?;
    let units = [
        ("repo", Some("repo")),
        ("working", Some("working")),
        ("python", Some("python")),
        ("repo.git.migrating", None),
        ("repo.checkout.old", None),
        (".pip_update_needed.tmp", None),
    ];
    let mut operations = Vec::new();
    for (name, source) in units {
        let live = format!("{APP_ROOT}/{name}");
        let replacement = source
            .map(|source| stamp_path(&prepared.join(source)))
            .transpose()?
            .flatten();
        operations.push(SwapOperation {
            original: stamp_path(&path_under(root, &live)?)?,
            live: live.clone(),
            prepared: source
                .map(|source| format!("{STATE_ROOT}/prepared/{}/{source}", journal.transaction_id)),
            backup: format!(
                "data/backups/NoNo/{}/retired/{live}",
                journal.transaction_id
            ),
            replacement,
            intent: "pending".to_string(),
        });
    }
    for (live, source) in [
        ("pyappify.yml", "root/pyappify.yml"),
        ("build-info.json", "root/build-info.json"),
    ] {
        operations.push(SwapOperation {
            live: live.to_string(),
            original: stamp_path(&root.join(live))?,
            prepared: Some(format!(
                "{STATE_ROOT}/prepared/{}/{source}",
                journal.transaction_id
            )),
            backup: format!(
                "data/backups/NoNo/{}/retired/{live}",
                journal.transaction_id
            ),
            replacement: stamp_path(&prepared.join(source))?,
            intent: "pending".to_string(),
        });
    }
    journal.operations = operations;
    journal.stage = ReceiveStage::Prepared;
    save_journal(root, journal)?;
    checkpoint("prepared")
}

fn check_original_application(root: &Path, journal: &ReceiveJournal) -> Result<()> {
    for relative in [APP_ROOT, "pyappify.yml", "build-info.json"] {
        let original = journal.originals.get(relative);
        ensure!(
            stamp_path(&path_under(root, relative)?)?.as_ref() == original,
            "切换前原安装发生变化（E_ORIGINAL_CHANGED）"
        );
    }
    Ok(())
}

fn swap_units(root: &Path, journal: &mut ReceiveJournal) -> Result<()> {
    if journal.stage == ReceiveStage::Prepared {
        check_original_application(root, journal)?;
        journal.stage = ReceiveStage::Swapping;
        save_journal(root, journal)?;
    }
    for index in 0..journal.operations.len() {
        let operation = journal.operations[index].clone();
        let live = path_under(root, &operation.live)?;
        let backup = path_under(root, &operation.backup)?;
        let current = stamp_path(&live)?;
        let saved = stamp_path(&backup)?;
        // 更名后但状态落盘前中断时，以三个位置的字节身份确认已完成动作。
        if current == operation.replacement
            && (saved == operation.original || operation.original.is_none())
            && operation
                .prepared
                .as_ref()
                .is_none_or(|path| !root.join(path).exists())
        {
            journal.operations[index].intent = "completed".to_string();
            save_journal(root, journal)?;
            continue;
        }
        if saved.is_none() {
            ensure!(
                current == operation.original,
                "旧目录身份发生变化（E_SWAP_ORIGINAL）"
            );
            journal.operations[index].intent = "move_old".to_string();
            save_journal(root, journal)?;
            if current.is_some() {
                let relative_parent = backup
                    .parent()
                    .ok_or_else(|| anyhow!("备份路径无效（E_SWAP_PATH）"))?
                    .strip_prefix(root)?
                    .to_str()
                    .ok_or_else(|| anyhow!("备份路径无效（E_SWAP_PATH）"))?
                    .replace('\\', "/");
                ensure_directory(root, &relative_parent)?;
                rename_no_replace(root, &live, &backup)?;
                checkpoint(&format!("old_moved_{index}"))?;
            }
        } else {
            ensure!(
                saved == operation.original && current.is_none(),
                "交换状态不可证明（E_SWAP_RECOVERY）"
            );
        }
        if let Some(source) = &operation.prepared {
            let source = path_under(root, source)?;
            ensure!(
                stamp_path(&source)? == operation.replacement,
                "暂存目录身份发生变化（E_SWAP_PREPARED）"
            );
            journal.operations[index].intent = "move_new".to_string();
            save_journal(root, journal)?;
            if let Some(parent) = live.parent() {
                let relative = parent
                    .strip_prefix(root)?
                    .to_str()
                    .ok_or_else(|| anyhow!("运行目录无效（E_SWAP_PATH）"))?
                    .replace('\\', "/");
                if !relative.is_empty() {
                    ensure_directory(root, &relative)?;
                }
            }
            rename_no_replace(root, &source, &live)?;
            checkpoint(&format!("new_moved_{index}"))?;
        }
        ensure!(
            stamp_path(&live)? == operation.replacement,
            "交换结果不完整（E_SWAP_VERIFY）"
        );
        journal.operations[index].intent = "completed".to_string();
        save_journal(root, journal)?;
    }
    journal.stage = ReceiveStage::Committing;
    save_journal(root, journal)
}

fn commit_package(root: &Path, journal: &mut ReceiveJournal) -> Result<()> {
    for operation in &journal.operations {
        ensure!(
            stamp_path(&path_under(root, &operation.live)?)? == operation.replacement,
            "提交前受管内容发生变化（E_COMMIT_VERIFY）"
        );
    }
    verify_working(root, &journal.manifest)?;
    let candidate = read_object(&prepared_root(root, journal)?.join("app.json"))?
        .ok_or_else(|| anyhow!("缺少完整安装状态（E_COMMIT_APP）"))?;
    let live_app = path_under(root, &format!("{APP_ROOT}/app.json"))?;
    let original = original_path(root, journal, &format!("{APP_ROOT}/app.json"))?;
    let current = stamp_path(&live_app)?;
    let mut candidate_bytes = canonical_json(&candidate)?;
    candidate_bytes.push(b'\n');
    let expected = Some(PathStamp::File(FileStamp {
        size: candidate_bytes.len() as u64,
        sha256: digest_bytes(&candidate_bytes),
    }));
    ensure!(
        current == stamp_path(&original)? || current == expected,
        "提交期间应用偏好发生变化（E_COMMIT_APP_CHANGED）"
    );
    if current != expected {
        write_json(&live_app, &candidate)?;
    }
    checkpoint("app_saved")?;
    let receipt = AcceptedReceipt {
        schema_version: 1,
        bundle_id: journal.manifest.bundle_id.clone(),
        manifest_sha256: journal.manifest_sha256.clone(),
        transaction_id: journal.transaction_id.clone(),
        source_sha: journal.manifest.source_sha.clone(),
        base_update_version: journal.manifest.base_update_version.clone(),
        base_update_commit: journal.manifest.base_update_commit.clone(),
        launcher: journal.manifest.launcher.clone(),
        bootstrap_pending: true,
    };
    write_json(&state_path(root, "accepted.json")?, &receipt)?;
    checkpoint("receipt_saved")?;
    journal.stage = ReceiveStage::Accepted;
    save_journal(root, journal)
}

fn finish_cleanup(root: &Path, journal: &mut ReceiveJournal) -> Result<()> {
    let receipt: AcceptedReceipt = parse_json(&state_path(root, "accepted.json")?)?;
    ensure!(
        receipt.bundle_id == journal.manifest.bundle_id
            && receipt.manifest_sha256 == journal.manifest_sha256,
        "接收收据不匹配（E_RECEIPT_ID）"
    );
    journal.stage = ReceiveStage::CleanupPending;
    save_journal(root, journal)?;
    let pending = state_path(root, "receive.pending")?;
    if pending.exists() {
        fs::remove_file(pending).context("无法完成接收标记（E_RECEIPT_PENDING）")?;
    }
    let prepared = prepared_root(root, journal)?;
    if prepared.exists() {
        collect_tree(&prepared)?;
        if fs::remove_dir_all(&prepared).is_err() {
            // 原件备份永久保留；暂存清理失败留 journal 自动重试，不影响已提交安装。
            warn!("便携接收清理将在下次启动重试（E_CLEANUP_RETRY）");
            return Ok(());
        }
    }
    let history = ensure_directory(root, &format!("{STATE_ROOT}/history"))?
        .join(format!("{}.json", journal.transaction_id));
    if !history.exists() {
        rename_no_replace(root, &journal_path(root)?, &history)?;
    } else {
        bail!("接收历史已存在（E_HISTORY_CONFLICT）");
    }
    Ok(())
}

fn resume_transaction(root: &Path, journal: &mut ReceiveJournal) -> Result<()> {
    ensure!(
        journal.schema_version == 1
            && sha(&journal.manifest.bundle_id, 64)
            && journal
                .transaction_id
                .starts_with(&journal.manifest.bundle_id[..16]),
        "接收日志身份无效（E_JOURNAL_ID）"
    );
    relative_path(&journal.transaction_id)?;
    if journal.stage == ReceiveStage::Verified {
        protect_originals(root, journal)?;
    }
    if journal.stage == ReceiveStage::OriginalsProtected {
        migrate_user_data(root, journal)?;
    }
    if journal.stage == ReceiveStage::Protected {
        prepare_package(root, journal)?;
    }
    if matches!(
        journal.stage,
        ReceiveStage::Prepared | ReceiveStage::Swapping
    ) {
        swap_units(root, journal)?;
    }
    if journal.stage == ReceiveStage::Committing {
        commit_package(root, journal)?;
    }
    if matches!(
        journal.stage,
        ReceiveStage::Accepted | ReceiveStage::CleanupPending
    ) {
        finish_cleanup(root, journal)?;
    }
    Ok(())
}

fn preflight_existing_installation(root: &Path) -> Result<()> {
    let app = read_object(&path_under(root, &format!("{APP_ROOT}/app.json"))?)?;
    read_object(&path_under(root, "data/config/app_config.json")?)?;
    if let Some(app) = app {
        let app: App = serde_json::from_value(Value::Object(app))
            .map_err(|_| anyhow!("应用状态格式无效，原件已保留（E_APP_JSON_FORMAT）"))?;
        ensure!(
            !app.profiles.is_empty(),
            "应用运行配置缺失，原件已保留（E_APP_PROFILE）"
        );
        if app.installed {
            for file in ["python.exe", "python3.dll", "python312.dll"] {
                ensure!(
                    path_under(root, &format!("{APP_ROOT}/python/{file}"))?.is_file(),
                    "完整便携环境缺失，禁止清理原安装（E_RUNTIME_MISSING）"
                );
            }
        }
    }
    for marker in [
        "repo/.git",
        "repo.git.migrating",
        "working/.pip_update_needed.tmp",
    ] {
        ensure!(
            !path_under(root, &format!("{APP_ROOT}/{marker}"))?.exists(),
            "旧安装或失败标记必须通过完整便携包恢复（E_LEGACY_PACKAGE_REQUIRED）"
        );
    }
    Ok(())
}

pub(crate) async fn receive_portable_package(portable_root: &Path) -> Result<ReceiveOutcome> {
    let root = canonical_root(portable_root)?;
    let selected_path = path_under(&root, &format!("{INCOMING_ROOT}/selected.json"))?;
    let journal_exists = journal_path(&root)?.exists();
    if !selected_path.exists() && !journal_exists {
        preflight_existing_installation(&root)?;
        let receipt_path = state_path(&root, "accepted.json")?;
        if receipt_path.exists() {
            let receipt: AcceptedReceipt = parse_json(&receipt_path)?;
            BOOTSTRAP_UPDATES_BLOCKED.store(receipt.bootstrap_pending, Ordering::Release);
        }
        return Ok(ReceiveOutcome::NoPackage);
    }
    let selected: Option<SelectedPackage> = if selected_path.exists() && !journal_exists {
        Some(parse_json(&selected_path)?)
    } else {
        None
    };
    if let Some(selected) = &selected {
        ensure!(
            selected.schema_version == 1
                && sha(&selected.bundle_id, 64)
                && sha(&selected.manifest_sha256, 64),
            "包选择文件无效（E_SELECTED_FORMAT）"
        );
        if !journal_exists && state_path(&root, "accepted.json")?.exists() {
            let receipt: AcceptedReceipt = parse_json(&state_path(&root, "accepted.json")?)?;
            if receipt.bundle_id == selected.bundle_id
                && receipt.manifest_sha256 == selected.manifest_sha256
            {
                BOOTSTRAP_UPDATES_BLOCKED.store(receipt.bootstrap_pending, Ordering::Release);
                preflight_existing_installation(&root)?;
                return Ok(ReceiveOutcome::AlreadyAccepted);
            }
        }
    }
    let _receiver = exclusive_lease(&root, "receive.lock")?;
    let _idle = exclusive_lease(&root, "runtime.lock")?;
    assert_no_running_process(&root)?;
    if journal_exists {
        let mut journal: ReceiveJournal = parse_json(&journal_path(&root)?)?;
        resume_transaction(&root, &mut journal)?;
    }
    // 新选择文件即使解压中断也不能拖住已准备好的旧事务恢复。
    let selected = if journal_exists && selected_path.exists() {
        let selected: SelectedPackage = parse_json(&selected_path)?;
        ensure!(
            selected.schema_version == 1
                && sha(&selected.bundle_id, 64)
                && sha(&selected.manifest_sha256, 64),
            "包选择文件无效（E_SELECTED_FORMAT）"
        );
        Some(selected)
    } else {
        selected
    };
    if let Some(selected) = selected {
        let receipt_path = state_path(&root, "accepted.json")?;
        let already = if receipt_path.exists() {
            let receipt: AcceptedReceipt = parse_json(&receipt_path)?;
            receipt.bundle_id == selected.bundle_id
                && receipt.manifest_sha256 == selected.manifest_sha256
        } else {
            false
        };
        if !already {
            let payload = path_under(&root, &format!("{INCOMING_ROOT}/{}", selected.bundle_id))?;
            let manifest_path = payload.join("manifest.json");
            ensure!(
                file_stamp(&manifest_path)?.sha256 == selected.manifest_sha256,
                "包选择摘要不匹配（E_SELECTED_DIGEST）"
            );
            let manifest = read_manifest(&manifest_path)?;
            ensure!(
                manifest.bundle_id == selected.bundle_id,
                "包选择身份不匹配（E_SELECTED_ID）"
            );
            verify_payload(&root, &payload, &manifest, true)?;
            let mut journal = new_journal(&root, manifest, selected.manifest_sha256)?;
            ensure_directory(&root, STATE_ROOT)?;
            write_json(
                &state_path(&root, "receive.pending")?,
                &serde_json::json!({"bundle_id": journal.manifest.bundle_id}),
            )?;
            save_journal(&root, &journal)?;
            checkpoint("journal_saved")?;
            info!("便携接收开始，阶段 verified");
            resume_transaction(&root, &mut journal)?;
        }
    }
    let receipt: AcceptedReceipt = parse_json(&state_path(&root, "accepted.json")?)?;
    BOOTSTRAP_UPDATES_BLOCKED.store(receipt.bootstrap_pending, Ordering::Release);
    info!("便携接收完成，阶段 accepted");
    Ok(ReceiveOutcome::Accepted)
}

pub(crate) fn bootstrap_updates_blocked() -> bool {
    BOOTSTRAP_UPDATES_BLOCKED.load(Ordering::Acquire)
}

pub(crate) fn complete_bootstrap() -> Result<()> {
    complete_bootstrap_at(&std::env::current_dir()?)
}

fn complete_bootstrap_at(root: &Path) -> Result<()> {
    let root = canonical_root(root)?;
    let _lease = exclusive_lease(&root, "receive.lock")?;
    let path = state_path(&root, "accepted.json")?;
    let mut receipt: AcceptedReceipt = parse_json(&path)?;
    receipt.bootstrap_pending = false;
    write_json(&path, &receipt)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture {
        root: PathBuf,
        manifest: BundleManifest,
        account: Vec<u8>,
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.root).unwrap();
        }
    }

    fn seal(mut manifest: BundleManifest) -> BundleManifest {
        manifest.bundle_id.clear();
        let mut value = serde_json::to_value(&manifest).unwrap();
        value.as_object_mut().unwrap().remove("bundle_id");
        manifest.bundle_id = digest_bytes(&canonical_json(&value).unwrap());
        manifest
    }

    fn write_file(root: &Path, relative: &str, bytes: &[u8]) {
        let path = root.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
    }

    fn fixture(legacy: bool) -> Fixture {
        let root = std::env::temp_dir().join(format!(
            "nono-receive-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let payload = root.join(format!("{INCOMING_ROOT}/preparing"));
        fs::create_dir_all(&payload).unwrap();
        write_file(&root, "NoNo.exe", b"MZ synthetic launcher");
        let yml = format!(
            "name: NoNo\nprofiles:\n  - name: China\n    main_script: selfhost/NoNo.exe\n    git_url: {OFFICIAL_ORIGIN}\n    requires_python: '3.12'\n    requirements: requirements.txt\n    pip_args: --no-deps\n"
        );
        for name in ["working/pyappify.yml", "root/pyappify.yml"] {
            write_file(&payload, name, yml.as_bytes());
        }
        for (name, bytes) in [
            ("working/main.py", b"current source".as_slice()),
            ("working/config.py", b"current configuration".as_slice()),
            ("working/requirements.txt", b"pyappify==1.0.20\n".as_slice()),
            ("working/selfhost/NoNo.exe", b"MZ native host".as_slice()),
            ("working/selfhost/ok_pin.json", b"{}".as_slice()),
            ("python/python.exe", b"MZ python".as_slice()),
            ("python/python3.dll", b"MZ stable ABI".as_slice()),
            ("python/python312.dll", b"MZ python runtime".as_slice()),
            (
                "root/build-info.json",
                br#"{"source_sha":"synthetic"}"#.as_slice(),
            ),
        ] {
            write_file(&payload, name, bytes);
        }
        let repo = git2::Repository::init_bare(payload.join("repo")).unwrap();
        let signature = git2::Signature::now("NoNo测试", "fixture@example.invalid").unwrap();
        let tree_id = repo.treebuilder(None).unwrap().write().unwrap();
        let tree = repo.find_tree(tree_id).unwrap();
        let commit = repo
            .commit(
                Some("HEAD"),
                &signature,
                &signature,
                "准备更新锚点替身",
                &tree,
                &[],
            )
            .unwrap();
        let object = repo.find_object(commit, None).unwrap();
        repo.tag("v3.7.20", &object, &signature, "准备版本替身", false)
            .unwrap();
        repo.remote("origin", OFFICIAL_ORIGIN).unwrap();
        drop(object);
        drop(tree);
        drop(repo);
        let mut app = strict_working_app(&payload.join("working")).unwrap();
        app.installed = true;
        app.current_version = Some("v3.7.20".to_string());
        app.available_versions = vec!["v3.7.20".to_string()];
        write_json(&payload.join("app-seed.json"), &app).unwrap();
        let stamp = collect_tree(&payload).unwrap();
        let manifest = seal(BundleManifest {
            schema_version: 1,
            bundle_id: String::new(),
            app_name: "NoNo".to_string(),
            build_kind: "commit_snapshot".to_string(),
            source_sha: "a".repeat(40),
            base_update_version: "v3.7.20".to_string(),
            base_update_commit: commit.to_string(),
            origin: OFFICIAL_ORIGIN.to_string(),
            requirements_sha256: file_stamp(&payload.join("working/requirements.txt"))
                .unwrap()
                .sha256,
            app_seed_file: "app-seed.json".to_string(),
            launcher: LauncherIdentity {
                version: "1.2.7".to_string(),
                source_sha: nono_build::SOURCE_SHA.to_string(),
                action_sha: nono_build::ACTION_SHA.to_string(),
                adaptation_sha256: nono_build::ADAPTATION_SHA256.to_string(),
                source_tree_sha256: nono_build::SOURCE_TREE_SHA256.to_string(),
                exe_sha256: file_stamp(&root.join("NoNo.exe")).unwrap().sha256,
            },
            working_files: collect_tree(&payload.join("working")).unwrap().files,
            payload_files: stamp.files,
            payload_directories: stamp.directories,
        });
        write_json(&payload.join("manifest.json"), &manifest).unwrap();
        let final_payload = root.join(format!("{INCOMING_ROOT}/{}", manifest.bundle_id));
        fs::rename(payload, final_payload).unwrap();
        write_json(
            &root.join(format!("{INCOMING_ROOT}/selected.json")),
            &SelectedPackage {
                schema_version: 1,
                bundle_id: manifest.bundle_id.clone(),
                manifest_sha256: file_stamp(&root.join(format!(
                    "{INCOMING_ROOT}/{}/manifest.json",
                    manifest.bundle_id
                )))
                .unwrap()
                .sha256,
            },
        )
        .unwrap();
        let account =
            br#"{"accounts":[{"id":"synthetic"}],"enabled":false,"extension":{"count":0}}"#
                .to_vec();
        if legacy {
            write_file(&root, "pyappify.yml", b"old root configuration");
            write_file(&root, "build-info.json", b"old root metadata");
            write_file(
                &root,
                &format!("{APP_ROOT}/working/Configs/account.JSON"),
                &account,
            );
            write_file(
                &root,
                &format!("{APP_ROOT}/working/Screenshots/原始截图.PNG"),
                b"synthetic screenshot",
            );
            write_file(
                &root,
                &format!("{APP_ROOT}/working/Logs/原始日志.TXT"),
                b"synthetic log",
            );
            write_file(
                &root,
                &format!("{APP_ROOT}/working/main.py"),
                b"user modified source",
            );
            write_file(
                &root,
                &format!("{APP_ROOT}/working/.pip_update_needed.tmp"),
                b"old marker",
            );
            write_file(
                &root,
                &format!("{APP_ROOT}/repo/.git/HEAD"),
                b"old non-bare Git",
            );
            write_file(
                &root,
                &format!("{APP_ROOT}/repo/configs/account.json"),
                br#"{"accounts":["stale"]}"#,
            );
            write_file(
                &root,
                &format!("{APP_ROOT}/python/user-extra.bin"),
                b"old python original",
            );
            write_file(
                &root,
                &format!("{APP_ROOT}/repo.git.migrating/HEAD"),
                b"old interrupted Git",
            );
            let mut old = serde_json::to_value(&app).unwrap();
            old["auto_start"] = Value::Bool(false);
            old["update_method"] = Value::String(UPDATE_METHOD_OPTION_AUTO_PRE_RELEASE.to_string());
            old["update_state"] = Value::String("failed".to_string());
            old["update_target_version"] = Value::String("v0.0.1".to_string());
            old["update_error"] = Value::String("合成旧错误".to_string());
            old["extension"] = serde_json::json!({"enabled": false});
            old["profiles"][0]["extension"] = serde_json::json!({"kept": 0});
            write_json(&root.join(format!("{APP_ROOT}/app.json")), &old).unwrap();
        }
        Fixture {
            root,
            manifest,
            account,
        }
    }

    fn assert_received(fixture: &Fixture) {
        let app = fixture.root.join(APP_ROOT);
        assert_eq!(
            collect_tree(&app.join("working")).unwrap().files,
            fixture.manifest.working_files
        );
        let state = read_object(&app.join("app.json")).unwrap().unwrap();
        assert_eq!(state["auto_start"], false);
        assert_eq!(state["update_state"], "idle");
        assert_eq!(state["update_target_version"], Value::Null);
        assert_eq!(state["update_error"], Value::Null);
        assert_eq!(state["extension"], serde_json::json!({"enabled": false}));
        assert_eq!(
            state["profiles"][0]["extension"],
            serde_json::json!({"kept": 0})
        );
        assert_eq!(
            fs::read(
                fixture
                    .root
                    .join(format!("{USER_ROOT}/configs/account.json"))
            )
            .unwrap(),
            fixture.account
        );
        let user_tree = collect_tree(&fixture.root.join(USER_ROOT)).unwrap();
        assert!(user_tree.files.contains_key("configs/account.JSON"));
        assert!(user_tree.files.contains_key("screenshots/原始截图.PNG"));
        assert!(user_tree.files.contains_key("logs/原始日志.TXT"));
        assert!(!app.join("repo/.git").exists());
        assert!(!app.join("repo.git.migrating").exists());
    }

    #[tokio::test(flavor = "current_thread")]
    async fn real_directories_preserve_data_and_resume_every_commit_boundary() {
        // 新安装没有旧原件，也必须形成保护记录后才能准备和提交完整环境。
        for point in [
            "journal_saved",
            "originals_protected",
            "protected",
            "candidate_ready",
            "prepared",
            "app_saved",
            "receipt_saved",
            "new_moved_0",
            "new_moved_1",
            "new_moved_2",
            "new_moved_6",
            "new_moved_7",
        ] {
            let fixture = fixture(false);
            TEST_INTERRUPTION.with(|target| *target.borrow_mut() = Some(point.to_string()));
            assert!(
                receive_portable_package(&fixture.root).await.is_err(),
                "{point}"
            );
            TEST_INTERRUPTION.with(|target| *target.borrow_mut() = None);
            receive_portable_package(&fixture.root).await.unwrap();
            let state = read_object(&fixture.root.join(format!("{APP_ROOT}/app.json")))
                .unwrap()
                .unwrap();
            assert_eq!(state["installed"], true);
            assert_eq!(state["auto_start"], false);
            assert_eq!(state["current_profile"], "China");
            assert_eq!(
                state["current_version"],
                fixture.manifest.base_update_version
            );
            assert_eq!(
                collect_tree(&fixture.root.join(format!("{APP_ROOT}/working")))
                    .unwrap()
                    .files,
                fixture.manifest.working_files
            );
            let receipt: AcceptedReceipt =
                parse_json(&state_path(&fixture.root, "accepted.json").unwrap()).unwrap();
            assert!(receipt.bootstrap_pending);
            let protection = read_object(&fixture.root.join(format!(
                "data/backups/NoNo/{}/protection.json",
                receipt.transaction_id
            )))
            .unwrap()
            .unwrap();
            assert_eq!(protection["complete"], true);
            assert_eq!(protection["originals"], serde_json::json!({}));
            let before = fs::read(fixture.root.join(format!("{APP_ROOT}/app.json"))).unwrap();
            receive_portable_package(&fixture.root).await.unwrap();
            assert_eq!(
                fs::read(fixture.root.join(format!("{APP_ROOT}/app.json"))).unwrap(),
                before
            );
        }
        let mut points = vec![
            "journal_saved".to_string(),
            "original_copied".to_string(),
            "originals_protected".to_string(),
            "protected".to_string(),
            "candidate_ready".to_string(),
            "prepared".to_string(),
            "app_saved".to_string(),
            "receipt_saved".to_string(),
        ];
        for index in [0, 1, 2, 3, 6, 7] {
            points.push(format!("old_moved_{index}"));
        }
        for index in [0, 1, 2, 6, 7] {
            points.push(format!("new_moved_{index}"));
        }
        for point in points {
            let fixture = fixture(true);
            TEST_INTERRUPTION.with(|target| *target.borrow_mut() = Some(point.clone()));
            assert!(
                receive_portable_package(&fixture.root).await.is_err(),
                "{point}"
            );
            TEST_INTERRUPTION.with(|target| *target.borrow_mut() = None);
            receive_portable_package(&fixture.root).await.unwrap();
            assert_received(&fixture);
            let json = fixture.root.join(format!("{APP_ROOT}/app.json"));
            let mut changed = read_object(&json).unwrap().unwrap();
            changed.insert("auto_start".to_string(), Value::Bool(true));
            write_json(&json, &changed).unwrap();
            let before = fs::read(&json).unwrap();
            receive_portable_package(&fixture.root).await.unwrap();
            assert_eq!(fs::read(&json).unwrap(), before);
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn prepared_recovery_does_not_depend_on_incoming_and_keeps_bootstrap_pending() {
        let fixture = fixture(true);
        TEST_INTERRUPTION.with(|target| *target.borrow_mut() = Some("prepared".to_string()));
        assert!(receive_portable_package(&fixture.root).await.is_err());
        fs::remove_dir_all(
            fixture
                .root
                .join(format!("{INCOMING_ROOT}/{}", fixture.manifest.bundle_id)),
        )
        .unwrap();
        receive_portable_package(&fixture.root).await.unwrap();
        assert_received(&fixture);
        let receipt: AcceptedReceipt =
            parse_json(&state_path(&fixture.root, "accepted.json").unwrap()).unwrap();
        assert!(receipt.bootstrap_pending);
        complete_bootstrap_at(&fixture.root).unwrap();
        let receipt: AcceptedReceipt =
            parse_json(&state_path(&fixture.root, "accepted.json").unwrap()).unwrap();
        assert!(!receipt.bootstrap_pending);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn new_selected_restores_old_transaction_before_receiving_new_bytes() {
        let mut fixture = fixture(true);
        TEST_INTERRUPTION.with(|target| *target.borrow_mut() = Some("protected".to_string()));
        assert!(receive_portable_package(&fixture.root).await.is_err());
        let incoming = fixture.root.join(INCOMING_ROOT);
        let source = incoming.join(&fixture.manifest.bundle_id);
        let next = incoming.join("next-preparing");
        copy_tree(
            &fixture.root,
            &source,
            &next,
            &collect_tree(&source).unwrap(),
        )
        .unwrap();
        write_file(&fixture.root, "NoNo.exe", b"MZ next compiled launcher");
        write_file(&next, "working/main.py", b"next current source");
        fs::remove_file(next.join("manifest.json")).unwrap();
        let stamp = collect_tree(&next).unwrap();
        let mut manifest = fixture.manifest.clone();
        manifest.source_sha = "b".repeat(40);
        manifest.launcher.exe_sha256 = file_stamp(&fixture.root.join("NoNo.exe")).unwrap().sha256;
        manifest.working_files = collect_tree(&next.join("working")).unwrap().files;
        manifest.payload_files = stamp.files;
        manifest.payload_directories = stamp.directories;
        manifest = seal(manifest);
        write_json(&next.join("manifest.json"), &manifest).unwrap();
        let selected = SelectedPackage {
            schema_version: 1,
            bundle_id: manifest.bundle_id.clone(),
            manifest_sha256: file_stamp(&next.join("manifest.json")).unwrap().sha256,
        };
        fs::rename(next, incoming.join(&manifest.bundle_id)).unwrap();
        write_json(&incoming.join("selected.json"), &selected).unwrap();
        fixture.manifest = manifest;
        receive_portable_package(&fixture.root).await.unwrap();
        assert_received(&fixture);
        let receipt: AcceptedReceipt =
            parse_json(&state_path(&fixture.root, "accepted.json").unwrap()).unwrap();
        assert_eq!(receipt.source_sha, "b".repeat(40));
        assert_eq!(
            fs::read(fixture.root.join(format!("{APP_ROOT}/working/main.py"))).unwrap(),
            b"next current source"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn corrupt_payload_and_busy_runtime_refuse_live_writes() {
        let fixture = fixture(true);
        let before = collect_tree(&fixture.root.join(APP_ROOT)).unwrap();
        let lease = exclusive_lease(&fixture.root, "runtime.lock").unwrap();
        assert!(receive_portable_package(&fixture.root).await.is_err());
        drop(lease);
        assert_eq!(collect_tree(&fixture.root.join(APP_ROOT)).unwrap(), before);
        write_file(
            &fixture.root,
            &format!(
                "{INCOMING_ROOT}/{}/working/main.py",
                fixture.manifest.bundle_id
            ),
            b"broken payload",
        );
        assert!(receive_portable_package(&fixture.root).await.is_err());
        assert_eq!(collect_tree(&fixture.root.join(APP_ROOT)).unwrap(), before);
        assert!(!fixture.root.join("data/backups/NoNo").exists());
    }

    #[tokio::test(flavor = "current_thread")]
    async fn corrupted_json_is_backed_up_and_never_replaced_by_defaults() {
        for relative in ["app.json", "working/Configs/account.JSON"] {
            let fixture = fixture(true);
            let app = fixture.root.join(format!("{APP_ROOT}/{relative}"));
            fs::write(&app, b"{broken original json").unwrap();
            let before = collect_tree(&fixture.root.join(APP_ROOT)).unwrap();
            assert!(receive_portable_package(&fixture.root).await.is_err());
            assert_eq!(collect_tree(&fixture.root.join(APP_ROOT)).unwrap(), before);
            let journal: ReceiveJournal =
                parse_json(&journal_path(&fixture.root).unwrap()).unwrap();
            let original = if relative == "app.json" {
                original_path(&fixture.root, &journal, &format!("{APP_ROOT}/app.json")).unwrap()
            } else {
                original_path(&fixture.root, &journal, &format!("{APP_ROOT}/working"))
                    .unwrap()
                    .join("Configs/account.JSON")
            };
            assert_eq!(fs::read(original).unwrap(), b"{broken original json");
        }
    }

    #[test]
    fn manifest_keeps_release_anchor_and_payload_boundaries() {
        let fixture = fixture(false);
        let path = fixture.root.join("manifest-test.json");
        for version in ["v3.8.1-alpha.1", "v3.8.1-beta.1", "3.8.1.rc", "v3.8.1"] {
            let mut manifest = fixture.manifest.clone();
            manifest.build_kind = "release_tag".to_string();
            manifest.base_update_version = version.to_string();
            let manifest = seal(manifest);
            write_json(&path, &manifest).unwrap();
            assert!(read_manifest(&path).is_ok(), "{version}");
            if version.contains("alpha") || version.contains("beta") || version.contains("rc") {
                for kind in ["commit_snapshot", "working_install"] {
                    let mut snapshot = manifest.clone();
                    snapshot.build_kind = kind.to_string();
                    write_json(&path, &seal(snapshot)).unwrap();
                    assert!(read_manifest(&path).is_err());
                }
            }
        }
        for version in ["main", "v3.8", "v3.8.1-preview.1", "v3.8.1-beta.bad"] {
            let mut manifest = fixture.manifest.clone();
            manifest.build_kind = "release_tag".to_string();
            manifest.base_update_version = version.to_string();
            write_json(&path, &seal(manifest)).unwrap();
            assert!(read_manifest(&path).is_err(), "{version}");
        }
        for directory in [
            "data/config",
            "root/private",
            "working/configs",
            "working/Configs",
            "working/LOGS/empty",
            "python/.PIP_UPDATE_NEEDED.tmp",
        ] {
            let mut manifest = fixture.manifest.clone();
            manifest.payload_directories.insert(directory.to_string());
            write_json(&path, &seal(manifest)).unwrap();
            assert!(read_manifest(&path).is_err(), "{directory}");
        }
        for file in [
            "working/Configs/private.json",
            "working/.PIP_UPDATE_NEEDED.tmp",
        ] {
            let mut manifest = fixture.manifest.clone();
            manifest.payload_files.insert(
                file.to_string(),
                FileStamp {
                    size: 0,
                    sha256: digest_bytes(b""),
                },
            );
            write_json(&path, &seal(manifest)).unwrap();
            assert!(read_manifest(&path).is_err(), "{file}");
        }
    }
    #[tokio::test(flavor = "current_thread")]
    async fn accepted_receipt_does_not_bypass_missing_runtime_or_failed_marker_checks() {
        for missing_runtime in [true, false] {
            let fixture = fixture(true);
            receive_portable_package(&fixture.root).await.unwrap();
            if missing_runtime {
                fs::remove_file(fixture.root.join(format!("{APP_ROOT}/python/python.exe")))
                    .unwrap();
            } else {
                write_file(
                    &fixture.root,
                    &format!("{APP_ROOT}/working/.pip_update_needed.tmp"),
                    b"interrupted update",
                );
            }
            let before = collect_tree(&fixture.root.join(APP_ROOT)).unwrap();
            assert!(receive_portable_package(&fixture.root).await.is_err());
            assert_eq!(collect_tree(&fixture.root.join(APP_ROOT)).unwrap(), before);
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn complete_prepared_transaction_recovers_before_rejecting_partial_new_selected() {
        let fixture = fixture(true);
        TEST_INTERRUPTION.with(|target| *target.borrow_mut() = Some("prepared".to_string()));
        assert!(receive_portable_package(&fixture.root).await.is_err());
        write_file(
            &fixture.root,
            &format!("{INCOMING_ROOT}/selected.json"),
            b"{partial new selection",
        );
        assert!(receive_portable_package(&fixture.root).await.is_err());
        assert_received(&fixture);
        assert!(!state_path(&fixture.root, "receive.pending")
            .unwrap()
            .exists());
        let receipt: AcceptedReceipt =
            parse_json(&state_path(&fixture.root, "accepted.json").unwrap()).unwrap();
        assert_eq!(receipt.bundle_id, fixture.manifest.bundle_id);
    }
}
