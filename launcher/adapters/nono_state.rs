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
    root: PathBuf,
    handles: BTreeMap<PathBuf, File>,
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
    validate_directory_handle(&file)?;
    Ok(file)
}

fn validate_directory_handle(file: &File) -> Result<()> {
    let metadata = file
        .metadata()
        .context("无法读取已持目录身份（E_PATH_PIN）")?;
    ensure!(metadata.is_dir(), "父目录身份无效（E_PATH_PIN）");
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        ensure!(
            metadata.file_attributes() & 0x400 == 0,
            "父目录句柄包含重解析点（E_PATH_PIN）"
        );
    }
    Ok(())
}

impl DirectoryPins {
    fn new(root: &Path) -> Result<Self> {
        let mut handles = BTreeMap::new();
        handles.insert(root.to_path_buf(), open_directory(root)?);
        Ok(Self {
            root: root.to_path_buf(),
            handles,
        })
    }

    fn pin_parents(&mut self, paths: &[&Path]) -> Result<()> {
        for path in paths {
            ensure!(
                path.starts_with(&self.root) && *path != self.root,
                "路径超出便携目录（E_PATH_BOUNDARY）"
            );
            let mut missing = Vec::new();
            for parent in path.parent().into_iter().flat_map(Path::ancestors) {
                // 句柄阻止更名，却不能阻止原地设置 junction；缓存命中仍核对整条祖先链。
                if let Some(handle) = self.handles.get(parent) {
                    validate_directory_handle(handle)?;
                } else if parent.exists() {
                    missing.push(parent.to_path_buf());
                }
                if parent == self.root {
                    break;
                }
            }
            // 从已持祖先向下逐层获取，不能先打开子目录再补上祖先的保护。
            for parent in missing.into_iter().rev() {
                let handle = open_directory(&parent)?;
                self.handles.insert(parent, handle);
            }
        }
        Ok(())
    }

    fn validate_directory(&self, path: &Path) -> Result<()> {
        ensure!(
            path.starts_with(&self.root),
            "路径超出便携目录（E_PATH_BOUNDARY）"
        );
        for ancestor in path.ancestors() {
            let handle = self
                .handles
                .get(ancestor)
                .ok_or_else(|| anyhow!("目录尚未受保护（E_PATH_PIN）"))?;
            validate_directory_handle(handle)?;
            if ancestor == self.root {
                return Ok(());
            }
        }
        bail!("路径超出便携目录（E_PATH_BOUNDARY）")
    }

    fn ensure_directory(&mut self, relative: &str) -> Result<PathBuf> {
        let relative = relative_path(relative)?;
        self.validate_directory(&self.root)?;
        let mut current = self.root.clone();
        for component in relative.components() {
            let child = current.join(component);
            if self.handles.contains_key(&child) {
                self.validate_directory(&child)?;
            } else {
                self.pin_parents(&[&child])?;
                if child.exists() {
                    ensure!(
                        ordinary_metadata(&child)?.is_dir(),
                        "目录身份无效（E_PATH_DIRECTORY）"
                    );
                } else {
                    fs::create_dir(&child).context("无法创建事务目录（E_DIRECTORY_CREATE）")?;
                }
                let handle = open_directory(&child)?;
                self.handles.insert(child.clone(), handle);
            }
            current = child;
        }
        self.validate_directory(&current)?;
        Ok(current)
    }
}

pub(crate) fn pin_parents(root: &Path, paths: &[&Path]) -> Result<DirectoryPins> {
    let mut pins = DirectoryPins::new(root)?;
    pins.pin_parents(paths)?;
    Ok(pins)
}

pub(crate) fn ensure_directory(root: &Path, relative: &str) -> Result<PathBuf> {
    DirectoryPins::new(root)?.ensure_directory(relative)
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
    // 同次扫描持有已核验目录，每个目录只打开一次；退出后不向交换阶段遗留句柄。
    let mut pins = DirectoryPins::new(root)?;
    let mut pending = vec![root.to_path_buf()];
    while let Some(directory) = pending.pop() {
        pins.validate_directory(&directory)?;
        for entry in fs::read_dir(&directory).context("无法枚举原件（E_TREE_READ）")? {
            let path = entry?.path();
            let relative = path
                .strip_prefix(root)?
                .to_str()
                .ok_or_else(|| anyhow!("文件名编码无效（E_PATH_ENCODING）"))?
                .replace('\\', "/");
            relative_path(&relative)?;
            pins.pin_parents(&[&path])?;
            if ordinary_metadata(&path)?.is_dir() {
                let handle = open_directory(&path)?;
                pins.handles.insert(path.clone(), handle);
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
    let mut pins = DirectoryPins::new(root)?;
    copy_verified_with_pins(&mut pins, source, target, expected)
}

fn copy_verified_with_pins(
    pins: &mut DirectoryPins,
    source: &Path,
    target: &Path,
    expected: &FileStamp,
) -> Result<()> {
    let parent = target
        .parent()
        .ok_or_else(|| anyhow!("副本路径无效（E_COPY_PATH）"))?;
    let relative_parent = parent
        .strip_prefix(&pins.root)
        .ok()
        .and_then(Path::to_str)
        .ok_or_else(|| anyhow!("副本路径无效（E_COPY_PATH）"))?
        .replace('\\', "/");
    // 已预建的目标目录仍在下一步核对完整祖先；此处只避免逐文件重复创建流程。
    if !relative_parent.is_empty() && !pins.handles.contains_key(parent) {
        pins.ensure_directory(&relative_parent)?;
    }
    pins.pin_parents(&[source, target])?;
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
            rename_no_replace_with_pins(pins, &temporary, target)?;
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
    rename_no_replace_with_pins(pins, &temporary, target)?;
    Ok(())
}

pub(crate) fn copy_tree(
    root: &Path,
    source: &Path,
    target: &Path,
    expected: &TreeStamp,
) -> Result<()> {
    // 目录守卫只属于本次复制，既保护路径身份，也避免逐文件重开整条祖先链。
    let mut pins = DirectoryPins::new(root)?;
    let relative = target
        .strip_prefix(root)?
        .to_str()
        .ok_or_else(|| anyhow!("副本路径无效（E_COPY_PATH）"))?
        .replace('\\', "/");
    pins.ensure_directory(&relative)?;
    for directory in &expected.directories {
        pins.ensure_directory(&format!("{relative}/{directory}"))?;
    }
    for (relative, stamp) in &expected.files {
        copy_verified_with_pins(
            &mut pins,
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
    let mut pins = DirectoryPins::new(root)?;
    rename_no_replace_with_pins(&mut pins, source, target)
}

fn rename_no_replace_with_pins(
    pins: &mut DirectoryPins,
    source: &Path,
    target: &Path,
) -> Result<()> {
    pins.pin_parents(&[source, target])?;
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

impl ExclusiveLease {
    pub(crate) fn child_stdio(&self) -> Result<std::process::Stdio> {
        // 标准句柄继承同一文件对象，子进程核验真实锁路径，不依赖可伪造的跳过锁标记。
        Ok(std::process::Stdio::from(self._file.try_clone()?))
    }
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

    #[cfg(windows)]
    fn test_reparse_ioctl(
        directory: &Path,
        control: u32,
        payload: &mut [u8],
    ) -> std::io::Result<()> {
        use std::ffi::c_void;
        use std::os::windows::fs::OpenOptionsExt;
        use std::os::windows::io::AsRawHandle;

        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn DeviceIoControl(
                handle: *mut c_void,
                control: u32,
                input: *mut c_void,
                input_size: u32,
                output: *mut c_void,
                output_size: u32,
                returned: *mut u32,
                overlapped: *mut c_void,
            ) -> i32;
        }

        // 真实 WRITE_ATTRIBUTES 句柄可在目录 pin 仍持有时原地改变重解析属性。
        let handle = OpenOptions::new()
            .access_mode(0x100)
            .share_mode(0x1 | 0x2)
            .custom_flags(0x02000000 | 0x00200000)
            .open(directory)?;
        let mut returned = 0;
        let input_size = u32::try_from(payload.len()).unwrap();
        // SAFETY：File 保持有效目录句柄，输入和返回缓冲区在同步调用期间有效，无异步重叠。
        let result = unsafe {
            DeviceIoControl(
                handle.as_raw_handle(),
                control,
                payload.as_mut_ptr().cast(),
                input_size,
                std::ptr::null_mut(),
                0,
                &mut returned,
                std::ptr::null_mut(),
            )
        };
        if result == 0 {
            Err(std::io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    #[cfg(windows)]
    fn test_set_directory_junction(directory: &Path, target: &Path) -> std::io::Result<()> {
        let raw = target.to_string_lossy();
        let raw = raw.strip_prefix(r"\\?\").unwrap_or(&raw);
        let substitute = format!(r"\??\{raw}");
        let encode =
            |value: &str| -> Vec<u8> { value.encode_utf16().flat_map(u16::to_le_bytes).collect() };
        let substitute = encode(&substitute);
        let printed = encode(raw);
        let path_size = substitute.len() + printed.len() + 4;
        let mut data = Vec::new();
        data.extend(0xA0000003u32.to_le_bytes());
        for value in [
            path_size + 8,
            0,
            0,
            substitute.len(),
            substitute.len() + 2,
            printed.len(),
        ] {
            data.extend(u16::try_from(value).unwrap().to_le_bytes());
        }
        data.extend(substitute);
        data.extend([0, 0]);
        data.extend(printed);
        data.extend([0, 0]);
        test_reparse_ioctl(directory, 0x000900A4, &mut data)
    }

    #[cfg(windows)]
    fn test_delete_directory_reparse(directory: &Path, tag: u32) -> std::io::Result<()> {
        let mut data = Vec::from(tag.to_le_bytes());
        data.extend([0, 0, 0, 0]);
        test_reparse_ioctl(directory, 0x000900AC, &mut data)
    }

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
    fn batch_directory_pins_reuse_ancestors_and_release_before_directory_swap() {
        let root = root();
        let source = root.join("source/nested");
        fs::create_dir_all(&source).unwrap();
        let mut pins = DirectoryPins::new(&root).unwrap();
        for index in 0..64 {
            pins.pin_parents(&[&source.join(format!("file-{index}.bin"))])
                .unwrap();
        }
        // 文件数增加不会重开或累积重复祖先句柄，三个实际目录均持续受保护。
        assert_eq!(pins.handles.len(), 3);
        assert!(move_file(&root.join("source"), &root.join("renamed"), false).is_err());
        drop(pins);
        move_file(&root.join("source"), &root.join("renamed"), false).unwrap();
        assert!(root.join("renamed/nested").is_dir());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn cached_directory_guard_rejects_in_place_junction_before_descendant_access() {
        for populated in [false, true] {
            let root = root();
            let managed = root.join("managed");
            let ancestor = managed.join("ancestor");
            let outside = root.join("outside");
            fs::create_dir_all(&ancestor).unwrap();
            fs::create_dir_all(&outside).unwrap();
            if populated {
                fs::write(outside.join("existing.bin"), b"outside original").unwrap();
            }
            let source = managed.join("source.bin");
            fs::write(&source, b"source bytes").unwrap();
            let expected = file_stamp(&source).unwrap();
            let outside_before = collect_tree(&outside).unwrap();
            let target = ancestor.join("nested/data.bin");
            let mut pins = DirectoryPins::new(&managed).unwrap();
            pins.pin_parents(&[&target]).unwrap();
            test_set_directory_junction(&ancestor, &outside).unwrap();

            // 即使外部目标为空，read_dir 前的当前目录检查也不能被零项枚举绕过。
            let directory_rejected = pins.validate_directory(&ancestor).is_err();
            let descendant_rejected = pins.pin_parents(&[&target]).is_err();
            let creation_rejected = pins.ensure_directory("ancestor/nested").is_err();
            let copy_rejected =
                copy_verified_with_pins(&mut pins, &source, &target, &expected).is_err();
            let outside_unchanged = collect_tree(&outside)
                .map(|stamp| stamp == outside_before)
                .unwrap_or(false);

            // 必须先清除真实 junction 再释放 guard 和清理自有目录，不能跟随外部路径。
            test_delete_directory_reparse(&ancestor, 0xA0000003).unwrap();
            drop(pins);
            fs::remove_dir_all(root).unwrap();
            assert!(directory_rejected);
            assert!(descendant_rejected);
            assert!(creation_rejected);
            assert!(copy_rejected);
            assert!(outside_unchanged);
        }
    }

    #[cfg(windows)]
    #[test]
    fn cached_directory_guard_checks_ancestors_above_the_nearest_parent() {
        use std::os::windows::fs::MetadataExt;

        let root = root();
        let ancestor = root.join("ancestor");
        let nested = ancestor.join("nested");
        fs::create_dir_all(&nested).unwrap();
        fs::write(nested.join("original.bin"), b"original bytes").unwrap();
        let target = nested.join("new.bin");
        let mut pins = DirectoryPins::new(&root).unwrap();
        pins.pin_parents(&[&target]).unwrap();
        // Cloud RP 可原地附加到非空祖先，最近的 nested 句柄仍是普通目录。
        let tag = 0x9000001Au32;
        let mut data = Vec::from(tag.to_le_bytes());
        data.extend([0, 0, 0, 0]);
        test_reparse_ioctl(&ancestor, 0x000900A4, &mut data).unwrap();
        let nearest_still_ordinary = pins.handles[&nested]
            .metadata()
            .map(|metadata| metadata.is_dir() && metadata.file_attributes() & 0x400 == 0)
            .unwrap_or(false);
        let ancestor_changed = pins.handles[&ancestor]
            .metadata()
            .map(|metadata| metadata.file_attributes() & 0x400 != 0)
            .unwrap_or(false);
        let descendant_rejected = pins.pin_parents(&[&target]).is_err();
        let directory_rejected = pins.validate_directory(&nested).is_err();
        let creation_rejected = pins.ensure_directory("ancestor/nested").is_err();

        test_delete_directory_reparse(&ancestor, tag).unwrap();
        drop(pins);
        let original_preserved =
            fs::read(nested.join("original.bin")).unwrap() == b"original bytes";
        let no_new_file = !target.exists();
        fs::remove_dir_all(root).unwrap();
        assert!(nearest_still_ordinary);
        assert!(ancestor_changed);
        assert!(descendant_rejected);
        assert!(directory_rejected);
        assert!(creation_rejected);
        assert!(original_preserved);
        assert!(no_new_file);
    }

    #[test]
    fn batch_copy_keeps_empty_directories_and_rejects_existing_copy_drift() {
        let root = root();
        let source = root.join("source");
        fs::create_dir_all(source.join("nested/empty")).unwrap();
        fs::write(source.join("nested/data.bin"), b"original bytes").unwrap();
        let expected = collect_tree(&source).unwrap();
        let target = root.join("target");
        copy_tree(&root, &source, &target, &expected).unwrap();
        assert_eq!(collect_tree(&target).unwrap(), expected);
        // 同一副本可恢复重试，但已有冲突字节不能被新一轮复制覆盖。
        copy_tree(&root, &source, &target, &expected).unwrap();
        fs::write(target.join("nested/data.bin"), b"changed copy").unwrap();
        assert!(copy_tree(&root, &source, &target, &expected).is_err());
        assert_eq!(
            fs::read(target.join("nested/data.bin")).unwrap(),
            b"changed copy"
        );
        assert_eq!(
            fs::read(source.join("nested/data.bin")).unwrap(),
            b"original bytes"
        );
        // 错误返回也释放复制 guard，下一事务仍能进行整目录交换。
        move_file(&target, &root.join("retired"), false).unwrap();
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn batch_copy_rejects_hard_linked_source_and_recovers_its_partial_temporary() {
        let root = root();
        let source = root.join("source");
        fs::create_dir_all(&source).unwrap();
        let file = source.join("data.bin");
        fs::write(&file, b"complete original bytes").unwrap();
        let expected = collect_tree(&source).unwrap();
        let link = root.join("linked.bin");
        fs::hard_link(&file, &link).unwrap();
        let target = root.join("target");
        assert!(copy_tree(&root, &source, &target, &expected).is_err());
        assert!(!target.join("data.bin").exists());
        assert_eq!(fs::read(&file).unwrap(), b"complete original bytes");
        fs::remove_file(link).unwrap();
        let temporary = target.join(format!(".nono-copy-{}", digest_bytes(b"data.bin")));
        fs::write(&temporary, b"interrupted copy").unwrap();
        copy_tree(&root, &source, &target, &expected).unwrap();
        assert!(!temporary.exists());
        assert_eq!(collect_tree(&target).unwrap(), expected);
        move_file(&target, &root.join("completed"), false).unwrap();
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
