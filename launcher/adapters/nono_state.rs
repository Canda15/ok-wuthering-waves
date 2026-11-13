//! NoNo 安装事务与配置共用的文件边界；不读取或输出用户配置值。

use anyhow::{anyhow, bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct FileStamp {
    pub size: u64,
    pub sha256: String,
}

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct TreeStamp {
    pub files: BTreeMap<String, FileStamp>,
    pub directories: BTreeSet<String>,
}

pub(crate) fn digest_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

pub(crate) fn canonical_json(value: &impl Serialize) -> Result<Vec<u8>> {
    // 先转 Value，保证结构字段和映射键采用与打包器相同的排序。
    let value = serde_json::to_value(value).context("无法生成状态对象（E_STATE_ENCODE）")?;
    serde_json::to_vec(&value).context("无法生成状态字节（E_STATE_ENCODE）")
}

pub(crate) fn relative_path(value: &str) -> Result<PathBuf> {
    ensure!(
        !value.is_empty() && !value.contains('\\'),
        "路径格式无效（E_PATH_FORMAT）"
    );
    for component in value.split('/') {
        ensure!(
            !component.is_empty()
                && !matches!(component, "." | "..")
                && !component.ends_with(['.', ' '])
                && !component
                    .chars()
                    .any(|c| c.is_control() || ":<>\"|?*".contains(c)),
            "路径格式无效（E_PATH_FORMAT）"
        );
        let base = component
            .split('.')
            .next()
            .unwrap_or_default()
            .to_ascii_uppercase();
        let numbered_device = base.len() == 4
            && (base.starts_with("COM") || base.starts_with("LPT"))
            && matches!(base.as_bytes()[3], b'1'..=b'9');
        ensure!(
            !matches!(base.as_str(), "CON" | "PRN" | "AUX" | "NUL") && !numbered_device,
            "路径包含设备别名（E_PATH_DEVICE）"
        );
    }
    Ok(PathBuf::from(value))
}

fn ordinary_metadata(path: &Path) -> Result<fs::Metadata> {
    let metadata = fs::symlink_metadata(path).context("无法读取文件身份（E_PATH_METADATA）")?;
    ensure!(
        !metadata.file_type().is_symlink(),
        "路径包含链接（E_PATH_LINK）"
    );
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        ensure!(
            metadata.file_attributes() & 0x400 == 0,
            "路径包含重解析点（E_PATH_REPARSE）"
        );
    }
    ensure!(
        metadata.is_file() || metadata.is_dir(),
        "路径类型无效（E_PATH_TYPE）"
    );
    Ok(metadata)
}

pub(crate) fn canonical_root(root: &Path) -> Result<PathBuf> {
    for ancestor in root.ancestors() {
        ordinary_metadata(ancestor)?;
    }
    let root = root
        .canonicalize()
        .context("无法定位便携目录（E_ROOT_RESOLVE）")?;
    ensure!(
        root.is_dir() && root.parent().is_some(),
        "便携目录无效（E_ROOT_INVALID）"
    );
    Ok(root)
}

pub(crate) fn path_under(root: &Path, relative: &str) -> Result<PathBuf> {
    let path = root.join(relative_path(relative)?);
    for ancestor in path.ancestors() {
        if ancestor.exists() {
            ordinary_metadata(ancestor)?;
        }
        if ancestor == root {
            return Ok(path);
        }
    }
    bail!("路径超出便携目录（E_PATH_BOUNDARY）")
}

pub(crate) struct DirectoryPins {
    _handles: Vec<File>,
}

fn open_directory(path: &Path) -> Result<File> {
    ordinary_metadata(path)?;
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // 不共享 DELETE，持有父目录期间禁止更名或替换祖先目录。
        options
            .share_mode(0x1 | 0x2)
            .custom_flags(0x02000000 | 0x00200000);
    }
    let file = options.open(path).context("无法锁定父目录（E_PATH_PIN）")?;
    ensure!(file.metadata()?.is_dir(), "父目录身份无效（E_PATH_PIN）");
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        ensure!(
            file.metadata()?.file_attributes() & 0x400 == 0,
            "父目录句柄包含重解析点（E_PATH_PIN）"
        );
    }
    Ok(file)
}

pub(crate) fn pin_parents(root: &Path, paths: &[&Path]) -> Result<DirectoryPins> {
    let mut directories = BTreeSet::new();
    directories.insert(root.to_path_buf());
    for path in paths {
        ensure!(
            path.starts_with(root) && *path != root,
            "路径超出便携目录（E_PATH_BOUNDARY）"
        );
        for parent in path.parent().into_iter().flat_map(Path::ancestors) {
            if parent.exists() {
                directories.insert(parent.to_path_buf());
            }
            if parent == root {
                break;
            }
        }
    }
    let mut handles = Vec::new();
    for path in directories {
        handles.push(open_directory(&path)?);
    }
    Ok(DirectoryPins { _handles: handles })
}

pub(crate) fn ensure_directory(root: &Path, relative: &str) -> Result<PathBuf> {
    let relative = relative_path(relative)?;
    let mut current = root.to_path_buf();
    for component in relative.components() {
        let child = current.join(component);
        let _pins = pin_parents(root, &[&child])?;
        if child.exists() {
            ensure!(
                ordinary_metadata(&child)?.is_dir(),
                "目录身份无效（E_PATH_DIRECTORY）"
            );
        } else {
            fs::create_dir(&child).context("无法创建事务目录（E_DIRECTORY_CREATE）")?;
        }
        current = child;
    }
    Ok(current)
}

fn open_readonly_file(path: &Path) -> Result<File> {
    ensure!(
        ordinary_metadata(path)?.is_file(),
        "文件身份无效（E_FILE_TYPE）"
    );
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.share_mode(0x1).custom_flags(0x00200000);
    }
    let file = options.open(path).context("无法锁定原件（E_FILE_OPEN）")?;
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Storage::FileSystem::{
            GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
        };
        // SAFETY：Win32 输出结构只含整数，零初始化不创建引用或无效枚举。
        let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
        // SAFETY：句柄由 File 持有，输出缓冲区是完整 Win32 结构。
        let success = unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) };
        ensure!(
            success != 0
                && information.nNumberOfLinks == 1
                && information.dwFileAttributes & 0x400 == 0,
            "原件身份不可证明（E_FILE_IDENTITY）"
        );
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        ensure!(
            file.metadata()?.nlink() == 1,
            "原件包含硬链接（E_FILE_IDENTITY）"
        );
    }
    Ok(file)
}

pub(crate) fn file_stamp(path: &Path) -> Result<FileStamp> {
    let mut file = open_readonly_file(path)?;
    let mut hash = Sha256::new();
    let size = std::io::copy(&mut file, &mut hash)?;
    Ok(FileStamp {
        size,
        sha256: format!("{:x}", hash.finalize()),
    })
}

pub(crate) fn collect_tree(root: &Path) -> Result<TreeStamp> {
    ensure!(
        ordinary_metadata(root)?.is_dir(),
        "目录身份无效（E_TREE_ROOT）"
    );
    let mut result = TreeStamp::default();
    let mut pending = vec![root.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(&directory).context("无法枚举原件（E_TREE_READ）")? {
            let path = entry?.path();
            let relative = path
                .strip_prefix(root)?
                .to_str()
                .ok_or_else(|| anyhow!("文件名编码无效（E_PATH_ENCODING）"))?
                .replace('\\', "/");
            relative_path(&relative)?;
            let _pins = pin_parents(root, &[&path])?;
            if ordinary_metadata(&path)?.is_dir() {
                result.directories.insert(relative);
                pending.push(path);
            } else {
                result.files.insert(relative, file_stamp(&path)?);
            }
        }
    }
    Ok(result)
}

pub(crate) fn copy_verified(
    root: &Path,
    source: &Path,
    target: &Path,
    expected: &FileStamp,
) -> Result<()> {
    let relative_parent = target
        .parent()
        .and_then(|p| p.strip_prefix(root).ok())
        .and_then(Path::to_str)
        .ok_or_else(|| anyhow!("副本路径无效（E_COPY_PATH）"))?
        .replace('\\', "/");
    if !relative_parent.is_empty() {
        ensure_directory(root, &relative_parent)?;
    }
    let _pins = pin_parents(root, &[source, target])?;
    if target.exists() {
        ensure!(
            file_stamp(target)? == *expected,
            "原件副本发生变化（E_COPY_CONFLICT）"
        );
        return Ok(());
    }
    // 暂存文件只存在于本事务的新目录。中途终止后可丢弃部分副本，不能截断正式原件。
    let temporary = target.with_file_name(format!(
        ".nono-copy-{}",
        digest_bytes(
            target
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .as_bytes()
        )
    ));
    if temporary.exists() {
        if file_stamp(&temporary)? == *expected {
            rename_no_replace(root, &temporary, target)?;
            return Ok(());
        }
        ordinary_metadata(&temporary)?;
        fs::remove_file(&temporary).context("无法清理部分副本（E_COPY_RETRY）")?;
    }
    let mut input = open_readonly_file(source)?;
    let mut output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)
        .context("无法创建原件副本（E_COPY_CREATE）")?;
    let mut hash = Sha256::new();
    let mut size = 0;
    let mut buffer = [0u8; 65536];
    loop {
        let length = input.read(&mut buffer)?;
        if length == 0 {
            break;
        }
        output.write_all(&buffer[..length])?;
        hash.update(&buffer[..length]);
        size += length as u64;
    }
    output.flush()?;
    output.sync_all()?;
    drop(output);
    ensure!(
        FileStamp {
            size,
            sha256: format!("{:x}", hash.finalize())
        } == *expected,
        "复制期间原件发生变化（E_COPY_SOURCE_CHANGED）"
    );
    rename_no_replace(root, &temporary, target)?;
    Ok(())
}

pub(crate) fn copy_tree(
    root: &Path,
    source: &Path,
    target: &Path,
    expected: &TreeStamp,
) -> Result<()> {
    let relative = target
        .strip_prefix(root)?
        .to_str()
        .ok_or_else(|| anyhow!("副本路径无效（E_COPY_PATH）"))?
        .replace('\\', "/");
    ensure_directory(root, &relative)?;
    for directory in &expected.directories {
        ensure_directory(root, &format!("{relative}/{directory}"))?;
    }
    for (relative, stamp) in &expected.files {
        copy_verified(
            root,
            &source.join(relative_path(relative)?),
            &target.join(relative_path(relative)?),
            stamp,
        )?;
    }
    ensure!(
        collect_tree(target)? == *expected,
        "原件副本不完整（E_COPY_VERIFY）"
    );
    Ok(())
}

pub(crate) fn rename_no_replace(root: &Path, source: &Path, target: &Path) -> Result<()> {
    let _pins = pin_parents(root, &[source, target])?;
    ordinary_metadata(source)?;
    ensure!(!target.exists(), "交换目标已存在（E_SWAP_OCCUPIED）");
    // exists 只用于诊断；真正的“不覆盖”由系统同一更名操作保证。
    move_file(source, target, false).context("无法交换受管路径（E_SWAP_RENAME）")
}

pub(crate) fn write_bytes_atomically(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("状态路径无效（E_STATE_PATH）"))?;
    ordinary_metadata(parent)?;
    let _pin = open_directory(parent)?;
    if path.exists() {
        ordinary_metadata(path)?;
    }
    let (temporary, mut output) = loop {
        let sequence = TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let temporary = parent.join(format!(".nono-{}-{sequence}.tmp", std::process::id()));
        match OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)
        {
            Ok(file) => break (temporary, file),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error).context("无法创建状态临时文件（E_STATE_CREATE）"),
        }
    };
    let result = (|| {
        output.write_all(bytes)?;
        output.flush()?;
        output.sync_all()?;
        drop(output);
        move_file(&temporary, path, true)?;
        Ok::<(), anyhow::Error>(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result.context("无法提交状态文件（E_STATE_WRITE）")
}

#[cfg(windows)]
fn move_file(source: &Path, target: &Path, replace_existing: bool) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    let flags = MOVEFILE_WRITE_THROUGH
        | if replace_existing {
            MOVEFILE_REPLACE_EXISTING
        } else {
            0
        };
    // SAFETY：两个字符串均以 NUL 结尾且在调用期间有效；未启用跨卷复制。
    if unsafe { MoveFileExW(source.as_ptr(), target.as_ptr(), flags) } == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn move_file(source: &Path, target: &Path, replace_existing: bool) -> std::io::Result<()> {
    if replace_existing {
        fs::rename(source, target)
    } else {
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "目录交换仅支持 Windows",
        ))
    }
}

pub(crate) fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let mut bytes = canonical_json(value)?;
    bytes.push(b'\n');
    write_bytes_atomically(path, &bytes)
}

pub(crate) fn read_object(path: &Path) -> Result<Option<Map<String, Value>>> {
    if !path.exists() {
        return Ok(None);
    }
    let mut file = open_readonly_file(path)?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| anyhow!("JSON 格式无效，原件已保留（E_JSON_FORMAT）"))?;
    let object = value
        .as_object()
        .cloned()
        .ok_or_else(|| anyhow!("JSON 必须为对象，原件已保留（E_JSON_OBJECT）"))?;
    Ok(Some(object))
}

pub(crate) fn merge_app_object(
    mut original: Map<String, Value>,
    typed: Value,
) -> Result<Map<String, Value>> {
    let mut typed = typed
        .as_object()
        .cloned()
        .ok_or_else(|| anyhow!("应用状态无效（E_APP_OBJECT）"))?;
    let old_profiles = original
        .get("profiles")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if let Some(Value::Array(profiles)) = typed.get_mut("profiles") {
        for profile in profiles {
            let name = profile
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let mut merged = old_profiles
                .iter()
                .find(|old| old.get("name").and_then(Value::as_str) == Some(name))
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            // 旧别名与新规范字段同时存在会使 serde 报重复字段。
            merged.remove("uac");
            if let Some(fields) = profile.as_object() {
                merged.extend(fields.clone());
            }
            *profile = Value::Object(merged);
        }
    }
    // 省略的受管字段不能从旧 raw 对象复活。
    original.remove("current_version_missing");
    if !typed.contains_key("keep_releases") {
        original.remove("keep_releases");
    }
    original.extend(typed);
    Ok(original)
}

pub(crate) struct ExclusiveLease {
    _file: File,
    _pins: DirectoryPins,
}

pub(crate) fn exclusive_lease(root: &Path, name: &str) -> Result<ExclusiveLease> {
    ensure!(
        matches!(name, "receive.lock" | "runtime.lock"),
        "租约类别无效（E_LEASE_NAME）"
    );
    let directory = ensure_directory(root, "data/state/NoNo")?;
    let path = directory.join(name);
    if path.exists() {
        ordinary_metadata(&path)?;
    }
    let pins = pin_parents(root, &[&path])?;
    let mut options = OpenOptions::new();
    options.create(true).truncate(false).read(true).write(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.share_mode(0).custom_flags(0x00200000);
    }
    let file = options
        .open(path)
        .context("此目录正在运行或迁移，请退出后重试（E_LEASE_BUSY）")?;
    Ok(ExclusiveLease {
        _file: file,
        _pins: pins,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root() -> PathBuf {
        let root = std::env::current_dir()
            .unwrap()
            .join("build/nono-state-tests")
            .join(format!(
                "{}-{}",
                std::process::id(),
                TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
        fs::create_dir_all(&root).unwrap();
        root.canonicalize().unwrap()
    }

    #[test]
    fn atomic_save_preserves_colliding_temporary_original() {
        let root = root();
        let sequence = TEMP_SEQUENCE.load(Ordering::Relaxed);
        let existing = root.join(format!(".nono-{}-{sequence}.tmp", std::process::id()));
        fs::write(&existing, b"original temporary").unwrap();
        let target = root.join("app.json");
        fs::write(&target, b"old state").unwrap();
        write_bytes_atomically(&target, b"new state").unwrap();
        assert_eq!(fs::read(&existing).unwrap(), b"original temporary");
        assert_eq!(fs::read(&target).unwrap(), b"new state");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn no_replace_move_keeps_existing_target_and_source() {
        let root = root();
        let source = root.join("source");
        let target = root.join("target");
        fs::write(&source, b"new bytes").unwrap();
        fs::write(&target, b"old bytes").unwrap();
        assert!(move_file(&source, &target, false).is_err());
        assert_eq!(fs::read(&source).unwrap(), b"new bytes");
        assert_eq!(fs::read(&target).unwrap(), b"old bytes");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn merge_preserves_unknown_fields_false_and_removes_managed_aliases() {
        let original = serde_json::from_value(json_for_test()).unwrap();
        let typed = serde_json::json!({
            "name": "NoNo", "auto_start": false,
            "profiles": [{"name": "China", "admin": true, "requirements": "new.txt"}],
        });
        let merged = merge_app_object(original, typed).unwrap();
        assert_eq!(
            merged["extension"],
            serde_json::json!({"enabled": false, "count": 0})
        );
        assert_eq!(merged["auto_start"], false);
        assert!(merged.get("keep_releases").is_none());
        assert!(merged.get("current_version_missing").is_none());
        assert_eq!(
            merged["profiles"][0]["extension"],
            serde_json::json!({"flag": false})
        );
        assert!(merged["profiles"][0].get("uac").is_none());
        assert_eq!(merged["profiles"][0]["admin"], true);
    }

    fn json_for_test() -> Value {
        serde_json::json!({
            "name": "NoNo", "auto_start": true, "keep_releases": 9, "current_version_missing": true,
            "extension": {"enabled": false, "count": 0},
            "profiles": [{"name": "China", "uac": false, "requirements": "old.txt", "extension": {"flag": false}}],
        })
    }

    #[test]
    fn strict_reader_keeps_corrupt_bytes_and_rejects_links_and_path_aliases() {
        let root = root();
        let config = root.join("config.json");
        fs::write(&config, b"{broken original").unwrap();
        assert!(read_object(&config).is_err());
        assert_eq!(fs::read(&config).unwrap(), b"{broken original");
        for name in [
            "../escape",
            "C:/escape",
            "dir\\escape",
            "NUL.txt",
            "dir/alias.",
            "dir/file:stream",
        ] {
            assert!(relative_path(name).is_err());
        }
        let linked = root.join("linked.json");
        fs::hard_link(&config, &linked).unwrap();
        assert!(read_object(&linked).is_err());
        fs::remove_dir_all(root).unwrap();
    }
}
