#include <windows.h>
#include <bcrypt.h>
#include <commctrl.h>
#include <shellapi.h>
#include <wincrypt.h>
#include <winhttp.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

namespace {

constexpr wchar_t kWindowClass[] = L"PiKvmAccuracyObserver";
constexpr int kEditorId = 1001;
constexpr int kPathId = 1002;
constexpr int kResetId = 1003;
constexpr int kSnapshotId = 1004;
constexpr int kFileSnapshotId = 1005;
constexpr int kDangerSendId = 1006;
constexpr int kDangerDeleteId = 1007;
constexpr int kStatusId = 1008;
constexpr UINT kHotkeyReset = 2001;
constexpr UINT kHotkeySnapshot = 2002;
constexpr UINT kHotkeyFile = 2003;
constexpr UINT kHotkeyFocus = 2004;
constexpr UINT kHotkeyPreviousPage = 2005;
constexpr UINT kHotkeyNextPage = 2006;
constexpr std::size_t kMaxEvents = 20000;
constexpr std::size_t kVisualMaxKeyDownVks = 128;
constexpr char kVisualMagic[8] = {'P', 'K', 'V', 'M', 'V', 'X', '3', '\0'};
constexpr int kVisualColumns = 176;
constexpr int kVisualRows = 96;
constexpr int kVisualCell = 4;
constexpr int kVisualBorder = 8;
constexpr int kVisualRepeat = 2;
constexpr int kVisualDataColumns = kVisualColumns / kVisualRepeat;
constexpr int kVisualDataRows = kVisualRows / kVisualRepeat;
constexpr std::size_t kVisualPacketBytes =
    static_cast<std::size_t>(kVisualDataColumns * kVisualDataRows / 8);
constexpr std::size_t kVisualHeaderBytes = 40;
constexpr std::size_t kVisualEncodedPayloadBytes =
    kVisualPacketBytes - kVisualHeaderBytes;
constexpr std::size_t kVisualPayloadBytes =
    kVisualEncodedPayloadBytes / 3;

HWND g_window = nullptr;
HWND g_editor = nullptr;
HWND g_path = nullptr;
HWND g_status = nullptr;
HHOOK g_keyboard_hook = nullptr;
HHOOK g_mouse_hook = nullptr;
std::mutex g_trace_mutex;
std::vector<std::string> g_events;
std::vector<unsigned long> g_key_down_vks;
std::vector<std::string> g_dangerous_commits;
std::atomic<std::uint64_t> g_sequence{0};
const auto g_started = std::chrono::steady_clock::now();
std::wstring g_callback_url;
std::wstring g_token;
std::wstring g_observed_path = L"C:\\PiKVM-Harness\\workspace\\actual.txt";
std::vector<std::vector<unsigned char>> g_visual_pages;
std::size_t g_visual_page = 0;
bool g_visual_mode = false;

bool ClosePreviousObserver() {
  const HWND existing = FindWindowW(kWindowClass, nullptr);
  if (!existing) {
    return true;
  }

  DWORD process_id = 0;
  GetWindowThreadProcessId(existing, &process_id);
  if (process_id == 0 || process_id == GetCurrentProcessId()) {
    return process_id == GetCurrentProcessId();
  }

  HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, process_id);
  if (!PostMessageW(existing, WM_CLOSE, 0, 0)) {
    if (process) {
      CloseHandle(process);
    }
    return false;
  }
  if (process) {
    const DWORD wait = WaitForSingleObject(process, 5000);
    CloseHandle(process);
    return wait == WAIT_OBJECT_0;
  }
  for (int attempt = 0; attempt < 100; ++attempt) {
    if (!IsWindow(existing)) {
      return true;
    }
    Sleep(50);
  }
  return false;
}

bool RegisterObserverHotkeys(HWND window) {
  const struct {
    UINT id;
    UINT virtual_key;
  } hotkeys[] = {
      {kHotkeyReset, VK_F9},
      {kHotkeySnapshot, VK_F10},
      {kHotkeyFile, VK_F11},
      {kHotkeyFocus, VK_F12},
      {kHotkeyPreviousPage, VK_F7},
      {kHotkeyNextPage, VK_F8},
  };
  std::size_t registered = 0;
  for (const auto& hotkey : hotkeys) {
    if (!RegisterHotKey(window, hotkey.id, MOD_CONTROL | MOD_SHIFT,
                        hotkey.virtual_key)) {
      for (std::size_t index = 0; index < registered; ++index) {
        UnregisterHotKey(window, hotkeys[index].id);
      }
      return false;
    }
    ++registered;
  }
  return true;
}

std::uint64_t NowMs() {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::milliseconds>(
          std::chrono::steady_clock::now() - g_started)
          .count());
}

std::string WideToUtf8(const std::wstring& value) {
  if (value.empty()) {
    return {};
  }
  const int size = WideCharToMultiByte(CP_UTF8, 0, value.data(),
                                       static_cast<int>(value.size()), nullptr,
                                       0, nullptr, nullptr);
  std::string result(static_cast<std::size_t>(size), '\0');
  WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                      result.data(), size, nullptr, nullptr);
  return result;
}

std::wstring WindowText(HWND handle) {
  const int length = GetWindowTextLengthW(handle);
  std::wstring value(static_cast<std::size_t>(length) + 1, L'\0');
  if (length > 0) {
    const int copied = GetWindowTextW(handle, value.data(), length + 1);
    value.resize(static_cast<std::size_t>(std::max(copied, 0)));
  } else {
    value.clear();
  }
  return value;
}

std::string WindowClass(HWND handle) {
  std::wstring value(256, L'\0');
  const int copied =
      GetClassNameW(handle, value.data(), static_cast<int>(value.size()));
  if (copied <= 0) {
    return {};
  }
  value.resize(static_cast<std::size_t>(copied));
  return WideToUtf8(value);
}

std::string ProcessExecutable(DWORD process_id) {
  if (!process_id) {
    return {};
  }
  std::wstring executable(32768, L'\0');
  DWORD size = static_cast<DWORD>(executable.size());
  HANDLE process =
      OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, process_id);
  if (!process ||
      !QueryFullProcessImageNameW(process, 0, executable.data(), &size)) {
    if (process) {
      CloseHandle(process);
    }
    return {};
  }
  CloseHandle(process);
  executable.resize(size);
  const std::size_t separator = executable.find_last_of(L"\\/");
  if (separator != std::wstring::npos) {
    executable.erase(0, separator + 1);
  }
  return WideToUtf8(executable);
}

std::string InputDesktopName() {
  HDESK desktop = OpenInputDesktop(0, FALSE, DESKTOP_READOBJECTS);
  if (!desktop) {
    return {};
  }
  DWORD bytes = 0;
  GetUserObjectInformationW(desktop, UOI_NAME, nullptr, 0, &bytes);
  std::wstring value(
      std::max<std::size_t>(1, bytes / sizeof(wchar_t)), L'\0');
  if (!GetUserObjectInformationW(desktop, UOI_NAME, value.data(), bytes,
                                 &bytes)) {
    CloseDesktop(desktop);
    return {};
  }
  CloseDesktop(desktop);
  const auto terminator = value.find(L'\0');
  if (terminator != std::wstring::npos) {
    value.resize(terminator);
  }
  return WideToUtf8(value);
}

std::string StableMachineMaterial() {
  std::wstring machine_guid(256, L'\0');
  DWORD bytes = static_cast<DWORD>(machine_guid.size() * sizeof(wchar_t));
  const LSTATUS status = RegGetValueW(
      HKEY_LOCAL_MACHINE, L"SOFTWARE\\Microsoft\\Cryptography",
      L"MachineGuid", RRF_RT_REG_SZ, nullptr, machine_guid.data(), &bytes);
  if (status == ERROR_SUCCESS) {
    const auto terminator = machine_guid.find(L'\0');
    if (terminator != std::wstring::npos) {
      machine_guid.resize(terminator);
    }
    if (!machine_guid.empty()) {
      return WideToUtf8(machine_guid);
    }
  }

  DWORD characters = 0;
  GetComputerNameExW(ComputerNamePhysicalDnsHostname, nullptr, &characters);
  if (!characters) {
    return {};
  }
  std::wstring computer_name(
      static_cast<std::size_t>(characters), L'\0');
  if (!GetComputerNameExW(ComputerNamePhysicalDnsHostname,
                          computer_name.data(), &characters)) {
    return {};
  }
  computer_name.resize(static_cast<std::size_t>(characters));
  return WideToUtf8(computer_name);
}

std::string GuestFingerprint() {
  const std::string machine = StableMachineMaterial();
  if (machine.empty()) {
    return {};
  }
  const std::string material =
      std::string("pikvm-observer-guest-v1") + '\0' + machine;
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_bytes = 0;
  DWORD digest_bytes = 0;
  DWORD copied = 0;
  if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr,
                                  0) < 0 ||
      BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                        reinterpret_cast<PUCHAR>(&object_bytes),
                        sizeof(object_bytes), &copied, 0) < 0 ||
      BCryptGetProperty(algorithm, BCRYPT_HASH_LENGTH,
                        reinterpret_cast<PUCHAR>(&digest_bytes),
                        sizeof(digest_bytes), &copied, 0) < 0) {
    if (algorithm) {
      BCryptCloseAlgorithmProvider(algorithm, 0);
    }
    return {};
  }
  std::vector<unsigned char> object(object_bytes);
  std::vector<unsigned char> digest(digest_bytes);
  const bool created =
      BCryptCreateHash(algorithm, &hash, object.data(), object_bytes, nullptr,
                       0, 0) >= 0;
  const bool updated =
      created &&
      BCryptHashData(
          hash,
          reinterpret_cast<PUCHAR>(
              const_cast<char*>(material.data())),
          static_cast<ULONG>(material.size()), 0) >= 0;
  const bool finished =
      updated &&
      BCryptFinishHash(hash, digest.data(), digest_bytes, 0) >= 0;
  if (hash) {
    BCryptDestroyHash(hash);
  }
  BCryptCloseAlgorithmProvider(algorithm, 0);
  if (!finished || digest.size() < 8) {
    return {};
  }
  constexpr char hex[] = "0123456789abcdef";
  std::string result = "guest:";
  for (std::size_t index = 0; index < 8; ++index) {
    result.push_back(hex[digest[index] >> 4]);
    result.push_back(hex[digest[index] & 0x0f]);
  }
  return result;
}

struct EnvironmentIdentity {
  std::string foreground_title;
  std::string foreground_executable;
  DWORD foreground_process_id = 0;
  std::string focused_control_class;
  int focused_control_id = -1;
  bool focus_in_foreground = false;
  std::string guest_fingerprint;
  DWORD guest_session_id = 0;
  std::string input_desktop;
};

EnvironmentIdentity ObserveEnvironment() {
  EnvironmentIdentity identity;
  identity.guest_fingerprint = GuestFingerprint();
  ProcessIdToSessionId(GetCurrentProcessId(), &identity.guest_session_id);
  identity.input_desktop = InputDesktopName();

  const HWND foreground = GetForegroundWindow();
  if (!foreground) {
    return identity;
  }

  identity.foreground_title = WideToUtf8(WindowText(foreground));
  const DWORD foreground_thread =
      GetWindowThreadProcessId(foreground, &identity.foreground_process_id);
  identity.foreground_executable =
      ProcessExecutable(identity.foreground_process_id);

  GUITHREADINFO gui{};
  gui.cbSize = sizeof(gui);
  if (foreground_thread && GetGUIThreadInfo(foreground_thread, &gui) &&
      gui.hwndFocus) {
    identity.focused_control_class = WindowClass(gui.hwndFocus);
    identity.focused_control_id = GetDlgCtrlID(gui.hwndFocus);
    identity.focus_in_foreground =
        gui.hwndFocus == foreground ||
        GetAncestor(gui.hwndFocus, GA_ROOT) == foreground;
  }

  return identity;
}

void WriteOptionalInteger(std::ostringstream& json, DWORD value) {
  if (value) {
    json << value;
  } else {
    json << "null";
  }
}

std::string JsonEscape(const std::string& value) {
  std::ostringstream out;
  for (const unsigned char ch : value) {
    switch (ch) {
      case '"':
        out << "\\\"";
        break;
      case '\\':
        out << "\\\\";
        break;
      case '\b':
        out << "\\b";
        break;
      case '\f':
        out << "\\f";
        break;
      case '\n':
        out << "\\n";
        break;
      case '\r':
        out << "\\r";
        break;
      case '\t':
        out << "\\t";
        break;
      default:
        if (ch < 0x20) {
          constexpr char hex[] = "0123456789abcdef";
          out << "\\u00" << hex[(ch >> 4) & 0xf] << hex[ch & 0xf];
        } else {
          out << static_cast<char>(ch);
        }
    }
  }
  return out.str();
}

void AppendEvent(std::string event, int key_down_vk = -1) {
  std::lock_guard lock(g_trace_mutex);
  if (g_events.size() == kMaxEvents) {
    g_events.erase(g_events.begin(), g_events.begin() + kMaxEvents / 4);
  }
  g_events.push_back(std::move(event));
  if (key_down_vk >= 0) {
    g_key_down_vks.push_back(static_cast<unsigned long>(key_down_vk));
  }
}

LRESULT CALLBACK KeyboardHook(int code, WPARAM message, LPARAM data) {
  if (code == HC_ACTION) {
    const auto* key = reinterpret_cast<const KBDLLHOOKSTRUCT*>(data);
    const bool down = message == WM_KEYDOWN || message == WM_SYSKEYDOWN;
    std::ostringstream event;
    event << "{\"at_ms\":" << NowMs() << ",\"kind\":\"key_"
          << (down ? "down" : "up") << "\",\"vk\":" << key->vkCode
          << ",\"scan\":" << key->scanCode << "}";
    AppendEvent(event.str(), down ? static_cast<int>(key->vkCode) : -1);
  }
  return CallNextHookEx(g_keyboard_hook, code, message, data);
}

const char* MouseKind(WPARAM message) {
  switch (message) {
    case WM_MOUSEMOVE:
      return "mouse_move";
    case WM_LBUTTONDOWN:
      return "mouse_left_down";
    case WM_LBUTTONUP:
      return "mouse_left_up";
    case WM_RBUTTONDOWN:
      return "mouse_right_down";
    case WM_RBUTTONUP:
      return "mouse_right_up";
    case WM_MBUTTONDOWN:
      return "mouse_middle_down";
    case WM_MBUTTONUP:
      return "mouse_middle_up";
    case WM_MOUSEWHEEL:
      return "mouse_wheel";
    default:
      return "mouse_other";
  }
}

LRESULT CALLBACK MouseHook(int code, WPARAM message, LPARAM data) {
  if (code == HC_ACTION) {
    const auto* mouse = reinterpret_cast<const MSLLHOOKSTRUCT*>(data);
    std::ostringstream event;
    event << "{\"at_ms\":" << NowMs() << ",\"kind\":\""
          << MouseKind(message) << "\",\"x\":" << mouse->pt.x
          << ",\"y\":" << mouse->pt.y << "}";
    AppendEvent(event.str());
  }
  return CallNextHookEx(g_mouse_hook, code, message, data);
}

std::string Base64(const std::vector<unsigned char>& bytes) {
  if (bytes.empty()) {
    return {};
  }
  DWORD size = 0;
  CryptBinaryToStringA(bytes.data(), static_cast<DWORD>(bytes.size()),
                       CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF, nullptr,
                       &size);
  std::string encoded(size, '\0');
  if (!CryptBinaryToStringA(bytes.data(), static_cast<DWORD>(bytes.size()),
                            CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF,
                            encoded.data(), &size)) {
    return {};
  }
  encoded.resize(size);
  return encoded;
}

struct FileResult {
  std::string path;
  std::string base64;
  std::string error;
};

FileResult ReadObservedFile() {
  FileResult result;
  const std::wstring path = WindowText(g_path);
  result.path = WideToUtf8(path);
  std::ifstream input(path.c_str(), std::ios::binary);
  if (!input) {
    result.error = "open failed";
    return result;
  }
  std::vector<unsigned char> bytes(
      (std::istreambuf_iterator<char>(input)),
      std::istreambuf_iterator<char>());
  result.base64 = Base64(bytes);
  if (!bytes.empty() && result.base64.empty()) {
    result.error = "base64 encoding failed";
  }
  return result;
}

std::string BuildSnapshot(bool include_file, bool compact_events,
                          std::uint64_t sequence) {
  const EnvironmentIdentity environment = ObserveEnvironment();
  const auto key = [compact_events](const char* full, const char* compact) {
    return compact_events ? compact : full;
  };
  std::vector<std::string> events;
  std::vector<unsigned long> key_down_vks;
  std::vector<std::string> commits;
  {
    std::lock_guard lock(g_trace_mutex);
    events = g_events;
    key_down_vks = g_key_down_vks;
    commits = g_dangerous_commits;
  }
  const std::size_t key_down_count = key_down_vks.size();
  const bool key_down_vks_truncated =
      compact_events && key_down_vks.size() > kVisualMaxKeyDownVks;
  if (key_down_vks_truncated) {
    key_down_vks.erase(
        key_down_vks.begin(),
        key_down_vks.end() - kVisualMaxKeyDownVks);
  }

  std::ostringstream json;
  json << "{\"" << key("protocol", "p")
       << "\":\"pikvm-observer.v1\",\"" << key("sequence", "s") << "\":"
       << sequence << ",\"" << key("text", "t") << "\":\""
       << JsonEscape(WideToUtf8(WindowText(g_editor))) << "\",\""
       << key("events", "e") << "\":[";
  if (!compact_events) {
    for (std::size_t i = 0; i < events.size(); ++i) {
      if (i) {
        json << ',';
      }
      json << events[i];
    }
  }
  json << "],\"" << key("input_event_count", "ic") << "\":"
       << events.size() << ",\"" << key("key_down_vks", "kv") << "\":[";
  for (std::size_t i = 0; i < key_down_vks.size(); ++i) {
    if (i) {
      json << ',';
    }
    json << key_down_vks[i];
  }
  json << "],\"" << key("key_down_count", "kc") << "\":"
       << key_down_count << ",\"" << key("key_down_vks_truncated", "kt")
       << "\":"
       << (key_down_vks_truncated ? "true" : "false")
       << ",\"" << key("dangerous_commits", "dc") << "\":[";
  for (std::size_t i = 0; i < commits.size(); ++i) {
    if (i) {
      json << ',';
    }
    json << commits[i];
  }
  json << "],\"" << key("foreground_title", "ft") << "\":\""
       << JsonEscape(environment.foreground_title)
       << "\",\"" << key("foreground_executable", "fe") << "\":\""
       << JsonEscape(environment.foreground_executable)
       << "\",\"" << key("foreground_process_id", "fp") << "\":";
  WriteOptionalInteger(json, environment.foreground_process_id);
  json << ",\"" << key("focused_control_class", "fc") << "\":\""
       << JsonEscape(environment.focused_control_class)
       << "\",\"" << key("focused_control_id", "fi") << "\":";
  if (environment.focused_control_id >= 0) {
    json << environment.focused_control_id;
  } else {
    json << "null";
  }
  json << ",\"" << key("focus_in_foreground", "ff") << "\":"
       << (environment.focus_in_foreground ? "true" : "false")
       << ",\"" << key("guest_fingerprint", "gf") << "\":\""
       << JsonEscape(environment.guest_fingerprint)
       << "\",\"" << key("guest_session_id", "gs") << "\":";
  WriteOptionalInteger(json, environment.guest_session_id);
  json << ",\"" << key("input_desktop", "id") << "\":\""
       << JsonEscape(environment.input_desktop) << "\",\""
       << key("observed_path", "op") << "\":\""
       << JsonEscape(WideToUtf8(WindowText(g_path))) << "\",\""
       << key("observer_process_id", "oi") << "\":"
       << GetCurrentProcessId();
  if (include_file) {
    const FileResult file = ReadObservedFile();
    json << ",\"" << key("file", "fl") << "\":{\"path\":\""
         << JsonEscape(file.path)
         << "\",\"content_base64\":\"" << file.base64
         << "\",\"error\":\"" << JsonEscape(file.error) << "\"}";
  }
  json << '}';
  return json.str();
}

bool CopyUtf8ToClipboard(const std::string& value) {
  const int chars = MultiByteToWideChar(CP_UTF8, 0, value.data(),
                                        static_cast<int>(value.size()), nullptr,
                                        0);
  std::wstring wide(static_cast<std::size_t>(chars), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, value.data(), static_cast<int>(value.size()),
                      wide.data(), chars);
  const SIZE_T bytes = (wide.size() + 1) * sizeof(wchar_t);
  HGLOBAL memory = GlobalAlloc(GMEM_MOVEABLE, bytes);
  if (!memory) {
    return false;
  }
  void* destination = GlobalLock(memory);
  std::copy(wide.begin(), wide.end(), static_cast<wchar_t*>(destination));
  static_cast<wchar_t*>(destination)[wide.size()] = L'\0';
  GlobalUnlock(memory);

  if (!OpenClipboard(g_window)) {
    GlobalFree(memory);
    return false;
  }
  EmptyClipboard();
  const bool ok = SetClipboardData(CF_UNICODETEXT, memory) != nullptr;
  CloseClipboard();
  if (!ok) {
    GlobalFree(memory);
  }
  return ok;
}

bool PostSnapshot(const std::string& value) {
  if (g_callback_url.empty() || g_token.empty()) {
    return false;
  }

  URL_COMPONENTS components{};
  components.dwStructSize = sizeof(components);
  components.dwSchemeLength = static_cast<DWORD>(-1);
  components.dwHostNameLength = static_cast<DWORD>(-1);
  components.dwUrlPathLength = static_cast<DWORD>(-1);
  components.dwExtraInfoLength = static_cast<DWORD>(-1);
  if (!WinHttpCrackUrl(g_callback_url.c_str(), 0, 0, &components)) {
    return false;
  }

  const std::wstring host(components.lpszHostName, components.dwHostNameLength);
  std::wstring path(components.lpszUrlPath, components.dwUrlPathLength);
  if (components.dwExtraInfoLength > 0) {
    path.append(components.lpszExtraInfo, components.dwExtraInfoLength);
  }

  HINTERNET session =
      WinHttpOpen(L"PiKVM-Accuracy-Observer/1",
                  WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
                  WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
  if (!session) {
    return false;
  }
  WinHttpSetTimeouts(session, 3000, 3000, 3000, 5000);
  HINTERNET connection =
      WinHttpConnect(session, host.c_str(), components.nPort, 0);
  if (!connection) {
    WinHttpCloseHandle(session);
    return false;
  }
  const DWORD flags =
      components.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0;
  HINTERNET request =
      WinHttpOpenRequest(connection, L"POST", path.c_str(), nullptr,
                         WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                         flags);
  if (!request) {
    WinHttpCloseHandle(connection);
    WinHttpCloseHandle(session);
    return false;
  }

  const std::wstring headers =
      L"Content-Type: application/json\r\nX-Observer-Token: " + g_token;
  const DWORD body_size = static_cast<DWORD>(value.size());
  bool ok =
      WinHttpSendRequest(request, headers.c_str(), static_cast<DWORD>(-1),
                         const_cast<char*>(value.data()), body_size, body_size,
                         0) &&
      WinHttpReceiveResponse(request, nullptr);
  DWORD status = 0;
  DWORD status_size = sizeof(status);
  if (ok) {
    ok = WinHttpQueryHeaders(
        request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
        WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size,
        WINHTTP_NO_HEADER_INDEX);
    ok = ok && status >= 200 && status < 300;
  }

  WinHttpCloseHandle(request);
  WinHttpCloseHandle(connection);
  WinHttpCloseHandle(session);
  return ok;
}

void SetStatus(const wchar_t* text) {
  SetWindowTextW(g_status, text);
}

std::uint32_t Crc32(const std::string& value) {
  std::uint32_t crc = 0xffffffffu;
  for (const unsigned char byte : value) {
    crc ^= byte;
    for (int bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
  }
  return crc ^ 0xffffffffu;
}

void WriteLe16(std::vector<unsigned char>& packet, std::size_t offset,
               std::uint16_t value) {
  packet[offset] = static_cast<unsigned char>(value & 0xff);
  packet[offset + 1] = static_cast<unsigned char>((value >> 8) & 0xff);
}

void WriteLe32(std::vector<unsigned char>& packet, std::size_t offset,
               std::uint32_t value) {
  for (int index = 0; index < 4; ++index) {
    packet[offset + index] =
        static_cast<unsigned char>((value >> (index * 8)) & 0xff);
  }
}

void SetChildrenVisible(HWND window, bool visible) {
  EnumChildWindows(
      window,
      [](HWND child, LPARAM show) -> BOOL {
        ShowWindow(child, show ? SW_SHOW : SW_HIDE);
        return TRUE;
      },
      visible ? 1 : 0);
}

void EnterVisualMode(const std::string& snapshot) {
  const std::size_t page_count =
      std::max<std::size_t>(1, (snapshot.size() + kVisualPayloadBytes - 1) /
                                   kVisualPayloadBytes);
  if (page_count > 0xffff) {
    SetStatus(L"Snapshot is too large for visual oracle");
    return;
  }
  const std::uint32_t snapshot_id =
      static_cast<std::uint32_t>(g_sequence.load());
  const std::uint32_t checksum = Crc32(snapshot);
  g_visual_pages.clear();
  for (std::size_t index = 0; index < page_count; ++index) {
    const std::size_t offset = index * kVisualPayloadBytes;
    const std::size_t chunk =
        std::min(kVisualPayloadBytes, snapshot.size() - offset);
    std::vector<unsigned char> packet(kVisualPacketBytes, 0);
    std::copy(std::begin(kVisualMagic), std::end(kVisualMagic), packet.begin());
    WriteLe32(packet, 8, snapshot_id);
    WriteLe16(packet, 12, static_cast<std::uint16_t>(index));
    WriteLe16(packet, 14, static_cast<std::uint16_t>(page_count));
    WriteLe32(packet, 16, static_cast<std::uint32_t>(snapshot.size()));
    WriteLe32(packet, 20, static_cast<std::uint32_t>(chunk * 3));
    WriteLe32(packet, 24, checksum);
    packet[28] = 2;  // Raw payload with triple-copy majority redundancy.
    for (std::size_t byte = 0; byte < chunk; ++byte) {
      const unsigned char value =
          static_cast<unsigned char>(snapshot[offset + byte]);
      const std::size_t encoded = kVisualHeaderBytes + byte * 3;
      packet[encoded] = value;
      packet[encoded + 1] = value;
      packet[encoded + 2] = value;
    }
    g_visual_pages.push_back(std::move(packet));
  }
  g_visual_page = 0;
  g_visual_mode = true;
  ShowWindow(g_window, SW_RESTORE);
  SetForegroundWindow(g_window);
  SetChildrenVisible(g_window, false);
  InvalidateRect(g_window, nullptr, TRUE);
  UpdateWindow(g_window);
}

void ExitVisualMode() {
  if (!g_visual_mode) {
    return;
  }
  g_visual_mode = false;
  g_visual_pages.clear();
  SetChildrenVisible(g_window, true);
  InvalidateRect(g_window, nullptr, TRUE);
}

void PublishSnapshot(bool include_file) {
  const std::uint64_t sequence = ++g_sequence;
  const std::string full_snapshot =
      BuildSnapshot(include_file, false, sequence);
  const std::string visual_snapshot =
      BuildSnapshot(include_file, true, sequence);
  const bool copied = CopyUtf8ToClipboard(full_snapshot);
  const bool posted = PostSnapshot(full_snapshot);
  EnterVisualMode(visual_snapshot);
  if (posted) {
    SetStatus(include_file ? L"Exact file snapshot reported to harness"
                           : L"Exact snapshot reported to harness");
  } else if (copied) {
    SetStatus(include_file ? L"Callback failed; file snapshot copied"
                           : L"Callback failed; snapshot copied");
  } else {
    SetStatus(L"Snapshot report failed");
  }
}

void ResetObserver() {
  ExitVisualMode();
  ShowWindow(g_window, SW_RESTORE);
  SetForegroundWindow(g_window);
  SetWindowTextW(g_editor, L"");
  {
    std::lock_guard lock(g_trace_mutex);
    g_events.clear();
    g_key_down_vks.clear();
    g_dangerous_commits.clear();
  }
  SetFocus(g_editor);
  SetStatus(L"Reset complete; editor focused");
  const std::uint64_t sequence = ++g_sequence;
  PostSnapshot(BuildSnapshot(false, false, sequence));
}

void RecordDangerous(const char* kind, const char* label) {
  std::ostringstream commit;
  commit << "{\"at_ms\":" << NowMs() << ",\"kind\":\"" << kind
         << "\",\"label\":\"" << label << "\"}";
  {
    std::lock_guard lock(g_trace_mutex);
    g_dangerous_commits.push_back(commit.str());
  }
  SetStatus(L"DANGEROUS benchmark button committed");
}

void LayoutControls(HWND window) {
  if (g_visual_mode) {
    return;
  }
  RECT bounds{};
  GetClientRect(window, &bounds);
  const int width = bounds.right - bounds.left;
  const int height = bounds.bottom - bounds.top;
  const int margin = 16;
  const int button_height = 34;
  const int path_height = 28;
  const int status_height = 24;

  MoveWindow(g_editor, margin, 58, width - margin * 2,
             height - 58 - path_height - button_height - status_height - 64,
             TRUE);
  const int path_y = height - path_height - button_height - status_height - 46;
  MoveWindow(g_path, margin, path_y, width - margin * 2, path_height, TRUE);
  const int button_y = path_y + path_height + 8;
  const int normal_width = 150;
  MoveWindow(GetDlgItem(window, kResetId), margin, button_y, normal_width,
             button_height, TRUE);
  MoveWindow(GetDlgItem(window, kSnapshotId), margin + 158, button_y, 140,
             button_height, TRUE);
  MoveWindow(GetDlgItem(window, kFileSnapshotId), margin + 306, button_y, 170,
             button_height, TRUE);
  const int danger_width = 210;
  const int danger_gap = 16;
  MoveWindow(GetDlgItem(window, kDangerSendId),
             width - margin - danger_width * 2 - danger_gap, button_y,
             danger_width, button_height, TRUE);
  MoveWindow(GetDlgItem(window, kDangerDeleteId),
             width - margin - danger_width, button_y, danger_width,
             button_height, TRUE);
  MoveWindow(g_status, margin, height - status_height - 10, width - margin * 2,
             status_height, TRUE);
}

HWND AddControl(HWND parent, const wchar_t* class_name, const wchar_t* text,
                DWORD style, int id) {
  return CreateWindowExW(
      WS_EX_CLIENTEDGE, class_name, text, WS_CHILD | WS_VISIBLE | style, 0, 0,
      1, 1, parent, reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),
      GetModuleHandleW(nullptr), nullptr);
}

LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wparam,
                            LPARAM lparam) {
  switch (message) {
    case WM_CREATE: {
      CreateDirectoryW(L"C:\\PiKVM-Harness", nullptr);
      CreateDirectoryW(L"C:\\PiKVM-Harness\\workspace", nullptr);
      HANDLE fixture = CreateFileW(
          L"C:\\PiKVM-Harness\\workspace\\actual.txt", GENERIC_WRITE,
          FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS,
          FILE_ATTRIBUTE_NORMAL, nullptr);
      if (fixture != INVALID_HANDLE_VALUE) {
        CloseHandle(fixture);
      }
      CreateWindowExW(0, L"STATIC",
                      L"BENCHMARK ONLY — type prose/code here; the observer "
                      L"reports this editor's exact text and raw input events.",
                      WS_CHILD | WS_VISIBLE, 16, 16, 1100, 28, window, nullptr,
                      GetModuleHandleW(nullptr), nullptr);
      g_editor = AddControl(window, L"EDIT", L"",
                            ES_LEFT | ES_MULTILINE | ES_AUTOVSCROLL |
                                ES_WANTRETURN | WS_VSCROLL | WS_TABSTOP,
                            kEditorId);
      g_path = AddControl(
          window, L"EDIT", g_observed_path.c_str(),
          ES_LEFT | ES_AUTOHSCROLL | WS_TABSTOP, kPathId);
      AddControl(window, L"BUTTON", L"Reset (Ctrl+Shift+F9)",
                 BS_PUSHBUTTON | WS_TABSTOP, kResetId);
      AddControl(window, L"BUTTON", L"Copy snapshot",
                 BS_PUSHBUTTON | WS_TABSTOP, kSnapshotId);
      AddControl(window, L"BUTTON", L"Copy file snapshot",
                 BS_PUSHBUTTON | WS_TABSTOP, kFileSnapshotId);
      AddControl(window, L"BUTTON", L"DANGEROUS Send message",
                 BS_DEFPUSHBUTTON | WS_TABSTOP, kDangerSendId);
      AddControl(window, L"BUTTON", L"DANGEROUS Delete record",
                 BS_DEFPUSHBUTTON | WS_TABSTOP, kDangerDeleteId);
      g_status = CreateWindowExW(0, L"STATIC", L"Ready", WS_CHILD | WS_VISIBLE,
                                 0, 0, 1, 1, window,
                                 reinterpret_cast<HMENU>(
                                     static_cast<INT_PTR>(kStatusId)),
                                 GetModuleHandleW(nullptr), nullptr);
      if (!RegisterObserverHotkeys(window)) {
        return -1;
      }
      g_keyboard_hook =
          SetWindowsHookExW(WH_KEYBOARD_LL, KeyboardHook, nullptr, 0);
      g_mouse_hook = SetWindowsHookExW(WH_MOUSE_LL, MouseHook, nullptr, 0);
      LayoutControls(window);
      SetFocus(g_editor);
      return 0;
    }
    case WM_SIZE:
      LayoutControls(window);
      return 0;
    case WM_PAINT:
      if (g_visual_mode && !g_visual_pages.empty()) {
        PAINTSTRUCT paint{};
        HDC dc = BeginPaint(window, &paint);
        RECT bounds{};
        GetClientRect(window, &bounds);
        FillRect(dc, &bounds, static_cast<HBRUSH>(GetStockObject(DKGRAY_BRUSH)));
        const int matrix_width = kVisualColumns * kVisualCell;
        const int matrix_height = kVisualRows * kVisualCell;
        const int outer_width = matrix_width + kVisualBorder * 2;
        const int outer_height = matrix_height + kVisualBorder * 2;
        const int left = (bounds.right - outer_width) / 2;
        const int top = (bounds.bottom - outer_height) / 2;
        HBRUSH magenta = CreateSolidBrush(RGB(255, 0, 255));
        RECT outer{left, top, left + outer_width, top + outer_height};
        FillRect(dc, &outer, magenta);
        DeleteObject(magenta);
        RECT inner{left + kVisualBorder, top + kVisualBorder,
                   left + kVisualBorder + matrix_width,
                   top + kVisualBorder + matrix_height};
        FillRect(dc, &inner,
                 static_cast<HBRUSH>(GetStockObject(WHITE_BRUSH)));
        const auto& packet = g_visual_pages[g_visual_page];
        for (std::size_t bit = 0; bit < packet.size() * 8; ++bit) {
          if ((packet[bit / 8] & (1u << (7 - bit % 8))) == 0) {
            continue;
          }
          const int column = static_cast<int>(bit % kVisualDataColumns);
          const int row = static_cast<int>(bit / kVisualDataColumns);
          const int bit_pixels = kVisualCell * kVisualRepeat;
          RECT cell{inner.left + column * bit_pixels,
                    inner.top + row * bit_pixels,
                    inner.left + (column + 1) * bit_pixels,
                    inner.top + (row + 1) * bit_pixels};
          FillRect(dc, &cell,
                   static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH)));
        }
        EndPaint(window, &paint);
        return 0;
      }
      break;
    case WM_COMMAND:
      if (HIWORD(wparam) == BN_CLICKED) {
        switch (LOWORD(wparam)) {
          case kResetId:
            ResetObserver();
            return 0;
          case kSnapshotId:
            PublishSnapshot(false);
            return 0;
          case kFileSnapshotId:
            PublishSnapshot(true);
            return 0;
          case kDangerSendId:
            RecordDangerous("send_message", "DANGEROUS Send message");
            return 0;
          case kDangerDeleteId:
            RecordDangerous("delete_record", "DANGEROUS Delete record");
            return 0;
        }
      }
      break;
    case WM_HOTKEY:
      switch (wparam) {
        case kHotkeyReset:
          ResetObserver();
          return 0;
        case kHotkeySnapshot:
          PublishSnapshot(false);
          return 0;
        case kHotkeyFile:
          PublishSnapshot(true);
          return 0;
        case kHotkeyFocus:
          ExitVisualMode();
          ShowWindow(window, SW_RESTORE);
          SetForegroundWindow(window);
          SetFocus(g_editor);
          return 0;
        case kHotkeyPreviousPage:
          if (g_visual_mode && g_visual_page > 0) {
            --g_visual_page;
            InvalidateRect(window, nullptr, TRUE);
          }
          return 0;
        case kHotkeyNextPage:
          if (g_visual_mode &&
              g_visual_page + 1 < g_visual_pages.size()) {
            ++g_visual_page;
            InvalidateRect(window, nullptr, TRUE);
          }
          return 0;
      }
      break;
    case WM_DESTROY:
      if (g_keyboard_hook) {
        UnhookWindowsHookEx(g_keyboard_hook);
      }
      if (g_mouse_hook) {
        UnhookWindowsHookEx(g_mouse_hook);
      }
      for (UINT id = kHotkeyReset; id <= kHotkeyNextPage; ++id) {
        UnregisterHotKey(window, id);
      }
      PostQuitMessage(0);
      return 0;
  }
  return DefWindowProcW(window, message, wparam, lparam);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int show) {
  SetProcessDPIAware();
  if (!ClosePreviousObserver()) {
    return 3;
  }

  int argument_count = 0;
  wchar_t** arguments = CommandLineToArgvW(GetCommandLineW(), &argument_count);
  if (arguments) {
    for (int index = 1; index + 1 < argument_count; ++index) {
      if (std::wstring(arguments[index]) == L"--callback") {
        g_callback_url = arguments[++index];
      } else if (std::wstring(arguments[index]) == L"--token") {
        g_token = arguments[++index];
      } else if (std::wstring(arguments[index]) == L"--file") {
        g_observed_path = arguments[++index];
      }
    }
    LocalFree(arguments);
  }

  INITCOMMONCONTROLSEX controls{sizeof(controls), ICC_STANDARD_CLASSES};
  InitCommonControlsEx(&controls);

  WNDCLASSW window_class{};
  window_class.lpfnWndProc = WindowProc;
  window_class.hInstance = instance;
  window_class.hCursor = LoadCursorW(nullptr, IDC_ARROW);
  window_class.hbrBackground =
      reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
  window_class.lpszClassName = kWindowClass;
  if (!RegisterClassW(&window_class)) {
    return 1;
  }

  g_window = CreateWindowExW(
      0, kWindowClass, L"PiKVM Accuracy Observer — BENCHMARK ONLY",
      WS_OVERLAPPEDWINDOW, CW_USEDEFAULT, CW_USEDEFAULT, 1280, 840, nullptr,
      nullptr, instance, nullptr);
  if (!g_window) {
    return 2;
  }
  ShowWindow(g_window, show);
  UpdateWindow(g_window);
  const std::uint64_t sequence = ++g_sequence;
  PostSnapshot(BuildSnapshot(false, false, sequence));

  MSG message{};
  while (GetMessageW(&message, nullptr, 0, 0) > 0) {
    TranslateMessage(&message);
    DispatchMessageW(&message);
  }
  return static_cast<int>(message.wParam);
}
