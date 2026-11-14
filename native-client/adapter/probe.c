/* Version-guarded adapter research for our original-client test copy.
 * Loaded by its existing Lua package loader, on the game's Lua thread.
 * Probe is read-only. Capture mode temporarily aliases a shared pointer's
 * object address while retaining its original ownership control block.
 * Only its two audited synchronous send calls use the local connector copy.
 * No injected thread, code patch, remote process access, or network setting.
 */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <ws2tcpip.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include "guards.h"

static uintptr_t image;
static DWORD owner_thread;
static uintptr_t attached_handler, original_connector;
static unsigned char local_connector[0x60];
static uintptr_t local_sdk_vtable[22];
static uintptr_t local_sdk;
static unsigned int captured_requests;
static ULONGLONG capture_generation;
static SOCKET local_socket = INVALID_SOCKET;
static int winsock_started, transport_mode;
struct pending_frame { struct pending_frame *next; size_t length, sent; unsigned char bytes[]; };
static struct pending_frame *outgoing_head, *outgoing_tail;
static size_t outgoing_bytes;
static unsigned char incoming[4194308];
static size_t incoming_bytes;

static int staged_client(void) {
    wchar_t filename[32768];
    DWORD length = GetModuleFileNameW(NULL, filename, sizeof(filename) / sizeof(filename[0]));
    const wchar_t dev_suffix[] = L"\\native-client\\runtime\\Dungeonborne\\Binaries\\Win64\\Dungeonborne.exe";
    const wchar_t player_suffix[] = L"\\native-client\\runtime-headless\\Dungeonborne\\Binaries\\Win64\\Dungeonborne.exe";
    const size_t dev_len = (sizeof(dev_suffix) / sizeof(dev_suffix[0])) - 1;
    const size_t player_len = (sizeof(player_suffix) / sizeof(player_suffix[0])) - 1;
    if (!length || length >= sizeof(filename) / sizeof(filename[0])) return 0;
    return (length > dev_len && _wcsicmp(filename + length - dev_len, dev_suffix) == 0) ||
           (length > player_len && _wcsicmp(filename + length - player_len, player_suffix) == 0);
}

static void record(const char *format, ...) {
    FILE *f = fopen("offline-native-adapter.log", "ab");
    if (!f) return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    fprintf(f, "%02u:%02u:%02u.%03u ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_list args;
    va_start(args, format);
    vfprintf(f, format, args);
    va_end(args);
    fputc('\n', f);
    fclose(f);
}

static int readable(const void *pointer, size_t bytes) {
    MEMORY_BASIC_INFORMATION m;
    if (!pointer || !VirtualQuery(pointer, &m, sizeof(m))) return 0;
    uintptr_t start = (uintptr_t)pointer;
    if (m.State != MEM_COMMIT || m.Protect & (PAGE_GUARD | PAGE_NOACCESS)) return 0;
    return start >= (uintptr_t)m.BaseAddress &&
        bytes <= m.RegionSize && start - (uintptr_t)m.BaseAddress <= m.RegionSize - bytes;
}

static uintptr_t pointer_at(uintptr_t start, size_t offset) {
    uintptr_t value = 0;
    if (start && offset <= UINTPTR_MAX - start && readable((void *)(start + offset), sizeof(value)))
        memcpy(&value, (void *)(start + offset), sizeof(value));
    return value;
}

static int guards_match(void) {
    if (!staged_client()) return 0;
    image = (uintptr_t)GetModuleHandleW(NULL);
    if (!image || !readable((void *)image, 0x1000)) return 0;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)image;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < 0 || dos->e_lfanew > 0xf00) return 0;
    IMAGE_NT_HEADERS64 *nt = (IMAGE_NT_HEADERS64 *)(image + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64) return 0;
    for (size_t i = 0; i < sizeof(GUARDS) / sizeof(GUARDS[0]); ++i) {
        const struct code_guard *g = &GUARDS[i];
        const void *p = (const void *)(image + g->rva);
        if (!readable(p, sizeof(g->bytes)) || memcmp(p, g->bytes, sizeof(g->bytes))) {
            record("ABSTAIN: code mismatch for %s", g->name);
            return 0;
        }
    }
    return 1;
}

static uintptr_t network_manager(void) {
    typedef void *(*getter)(void);
    typedef void *(*subsystem_getter)(void *, void *);
    uintptr_t gi = (uintptr_t)((getter)(image + RVA_GAME_INSTANCE))();
    void *type = ((getter)(image + RVA_NETWORK_CLASS))();
    if (!gi || !readable((void *)(gi + 0x100), 0x30) || !type) return 0;
    uintptr_t manager = (uintptr_t)((subsystem_getter)(image + RVA_GET_SUBSYSTEM))((void *)(gi + 0x100), type);
    if (!manager || pointer_at(manager, 0x10) != (uintptr_t)type || !readable((void *)manager, 0xa8)) return 0;
    return manager;
}

static int local_connected(void *self) {
    return self == &local_sdk && attached_handler && GetCurrentThreadId() == owner_thread &&
        (!transport_mode || local_socket != INVALID_SOCKET);
}

static uintptr_t unsupported_slot(void *self) {
    (void)self;
    record("ABSTAIN: unexpected local connector virtual call");
    return 0;
}

static int capture_request(void *self, const void *bytes, int length, int flag) {
    (void)flag;
    if (!local_connected(self) || length <= 0 || length > 4194304 || !readable(bytes, (size_t)length)) return 0;
    char temporary[150], final[150];
    unsigned int id = ++captured_requests;
    snprintf(temporary, sizeof(temporary), "offline-cs-request-%llu-%08u.tmp", capture_generation, id);
    snprintf(final, sizeof(final), "offline-cs-request-%llu-%08u.bin", capture_generation, id);
    FILE *file = fopen(temporary, "wb");
    if (!file) return 0;
    size_t written = fwrite(bytes, 1, (size_t)length, file);
    int close_result = fclose(file);
    if (written != (size_t)length || close_result || !MoveFileExA(temporary, final, MOVEFILE_WRITE_THROUGH)) return 0;
    record("captured original CSMsg: id=%u bytes=%d", id, length);
    if (transport_mode) {
        if (outgoing_bytes + (size_t)length + 4 > 8 * 1024 * 1024) return 0;
        struct pending_frame *frame = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(*frame) + (size_t)length + 4);
        if (!frame) return 0;
        frame->length = (size_t)length + 4;
        uint32_t network_length = htonl((uint32_t)length);
        memcpy(frame->bytes, &network_length, 4);
        memcpy(frame->bytes + 4, bytes, (size_t)length);
        if (outgoing_tail) outgoing_tail->next = frame;
        else outgoing_head = frame;
        outgoing_tail = frame;
        outgoing_bytes += frame->length;
    }
    return 1;
}

static void close_local_transport(void) {
    if (local_socket != INVALID_SOCKET) { closesocket(local_socket); local_socket = INVALID_SOCKET; }
    while (outgoing_head) {
        struct pending_frame *old = outgoing_head;
        outgoing_head = old->next;
        HeapFree(GetProcessHeap(), 0, old);
    }
    outgoing_tail = NULL;
    outgoing_bytes = incoming_bytes = 0;
    if (winsock_started) { WSACleanup(); winsock_started = 0; }
}

static int connect_loopback(void) {
    WSADATA data;
    if (WSAStartup(MAKEWORD(2,2), &data)) return 0;
    winsock_started = 1;
    local_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (local_socket == INVALID_SOCKET) goto fail;
    u_long nonblocking = 1;
    if (ioctlsocket(local_socket, FIONBIO, &nonblocking)) goto fail;
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_port = htons(18897);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(local_socket, (struct sockaddr *)&address, sizeof(address)) == SOCKET_ERROR && WSAGetLastError() != WSAEWOULDBLOCK) goto fail;
    fd_set ready;
    FD_ZERO(&ready);
    FD_SET(local_socket, &ready);
    struct timeval wait = {0, 250000};
    if (select(0, NULL, &ready, NULL, &wait) != 1) goto fail;
    int error = 0, size = sizeof(error);
    if (getsockopt(local_socket, SOL_SOCKET, SO_ERROR, (char *)&error, &size) || error) goto fail;
    nonblocking = 0;
    if (ioctlsocket(local_socket, FIONBIO, &nonblocking)) goto fail;
    DWORD timeout = 250;
    setsockopt(local_socket, SOL_SOCKET, SO_SNDTIMEO, (const char *)&timeout, sizeof(timeout));
    setsockopt(local_socket, SOL_SOCKET, SO_RCVTIMEO, (const char *)&timeout, sizeof(timeout));
    if (send(local_socket, "DBLOCAL1", 8, 0) != 8) goto fail;
    char hello[8];
    int received = 0;
    while (received < 8) {
        int count = recv(local_socket, hello + received, 8 - received, 0);
        if (count <= 0) goto fail;
        received += count;
    }
    if (memcmp(hello, "DBLOCAL1", 8)) goto fail;
    nonblocking = 1;
    if (ioctlsocket(local_socket, FIONBIO, &nonblocking)) goto fail;
    record("connected to local native-protocol endpoint 127.0.0.1:18897");
    return 1;
fail:
    record("ABSTAIN: local endpoint handshake failed (%d)", WSAGetLastError());
    close_local_transport();
    return 0;
}

static int deliver_original_message(const unsigned char *packet, size_t bytes) {
    uintptr_t manager = network_manager();
    if (GetCurrentThreadId() != owner_thread || pointer_at(manager, 0x48) != attached_handler ||
        pointer_at(attached_handler, 0x1f8) != (uintptr_t)local_connector ||
        pointer_at(attached_handler, 0) != image + 0x832f4a0) return 0;
    HMODULE sdk = GetModuleHandleW(L"GCloud.dll");
    HMODULE core = GetModuleHandleW(L"GCloudCore.dll");
    if (!sdk || !core) return 0;
    typedef void *(*data_constructor)(void *, const unsigned char *, int);
    typedef void (*data_destructor)(void *);
    typedef void (*message_receiver)(void *, void *, void *, void *);
    uintptr_t native_size = pointer_at(image, 0x67c1228);
    uintptr_t data_vtable = pointer_at(original_connector, 0x10);
    // This client's connector reads a binary GCloud::AString. AData has a
    // different layout; treating it as AString produced an empty message.
    uintptr_t gcloud_vtable = (uintptr_t)GetProcAddress(sdk, "??_7AString@GCloud@@6B@");
    uintptr_t core_vtable = (uintptr_t)GetProcAddress(core, "??_7AString@ABase@@6B@");
    data_constructor construct;
    data_destructor destroy;
    if (data_vtable == gcloud_vtable) {
        construct = (data_constructor)(uintptr_t)GetProcAddress(sdk, "??0AString@GCloud@@QEAA@PEBDH@Z");
        destroy = (data_destructor)(uintptr_t)GetProcAddress(sdk, "??1AString@GCloud@@UEAA@XZ");
    } else if (data_vtable == core_vtable) {
        construct = (data_constructor)(uintptr_t)GetProcAddress(core, "??0AString@ABase@@QEAA@PEBDH@Z");
        destroy = (data_destructor)(uintptr_t)GetProcAddress(core, "??1AString@ABase@@UEAA@XZ");
    } else {
        record("ABSTAIN: original data vtable=%p GCloud=%p Core=%p", (void *)data_vtable, (void *)gcloud_vtable, (void *)core_vtable);
        return 0;
    }
    if (!construct || !destroy || bytes > 4194304) return 0;
    unsigned char data_object[128] __attribute__((aligned(16))) = {0};
    construct(data_object, packet, (int)bytes);
    typedef int (*data_size)(void *);
    typedef const unsigned char *(*data_bytes)(void *);
    int verified_size = ((data_size)native_size)(data_object);
    uintptr_t native_data = pointer_at(image, 0x67c1230);
    const unsigned char *verified_bytes = ((data_bytes)native_data)(data_object);
    if (verified_size != (int)bytes || !readable(verified_bytes, bytes) || memcmp(verified_bytes, packet, bytes)) {
        record("ABSTAIN: original data accessor round trip failed: expected=%zu actual=%d", bytes, verified_size);
        destroy(data_object);
        return 0;
    }
    ((message_receiver)(image + RVA_RECEIVE_MESSAGE))((void *)attached_handler, NULL, NULL, data_object);
    destroy(data_object);
    record("delivered original CSMsg on game thread: bytes=%zu", bytes);
    return 1;
}

static int deliver_local_action(const unsigned char *packet, size_t bytes) {
    /* This is adapter control data, not Lua source or an original CSMsg.
       Only the Lua bridge's fixed action grammar may interpret it. */
    if (bytes < 10 || bytes > 1024 || memcmp(packet, "DBACTION1\n", 10)) return 0;
    for (size_t i = 0; i < bytes; ++i) {
        unsigned char c = packet[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '\n' || c == '/' || c == '_' || c == '-')) {
            record("ABSTAIN: invalid local action data");
            return -1;
        }
    }
    FILE *stream = fopen("offline-native-action.tmp", "wb");
    if (!stream) return -1;
    int ok = fwrite(packet, 1, bytes, stream) == bytes;
    if (fclose(stream)) ok = 0;
    if (!ok || !MoveFileExA("offline-native-action.tmp", "offline-native-action.txt", MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        DeleteFileA("offline-native-action.tmp");
        record("ABSTAIN: could not commit local action data");
        return -1;
    }
    record("received bounded local action data: bytes=%zu", bytes);
    return 1;
}

__declspec(dllexport) int offline_adapter_poll(void *lua_state) {
    (void)lua_state;
    if (!transport_mode || !attached_handler || local_socket == INVALID_SOCKET || GetCurrentThreadId() != owner_thread) return 0;
    size_t sent = 0;
    while (outgoing_head && sent < 131072) {
        struct pending_frame *frame = outgoing_head;
        int count = send(local_socket, (const char *)frame->bytes + frame->sent,
                         (int)(frame->length - frame->sent), 0);
        if (count == SOCKET_ERROR) {
            if (WSAGetLastError() == WSAEWOULDBLOCK) break;
            goto disconnected;
        }
        if (!count) goto disconnected;
        frame->sent += (size_t)count;
        sent += (size_t)count;
        if (frame->sent == frame->length) {
            outgoing_head = frame->next;
            if (!outgoing_head) outgoing_tail = NULL;
            outgoing_bytes -= frame->length;
            HeapFree(GetProcessHeap(), 0, frame);
        }
    }
    size_t received = 0;
    while (received < 131072) {
        if (incoming_bytes == sizeof(incoming)) goto disconnected;
        int count = recv(local_socket, (char *)incoming + incoming_bytes, (int)(sizeof(incoming) - incoming_bytes), 0);
        if (count == SOCKET_ERROR) {
            if (WSAGetLastError() == WSAEWOULDBLOCK) break;
            goto disconnected;
        }
        if (!count) goto disconnected;
        incoming_bytes += (size_t)count;
        received += (size_t)count;
        while (incoming_bytes >= 4) {
            uint32_t length;
            memcpy(&length, incoming, 4);
            length = ntohl(length);
            if (!length || length > 4194304) goto disconnected;
            if (incoming_bytes < (size_t)length + 4) break;
            int action = deliver_local_action(incoming + 4, length);
            if (action < 0 || (!action && !deliver_original_message(incoming + 4, length))) goto disconnected;
            incoming_bytes -= (size_t)length + 4;
            memmove(incoming, incoming + (size_t)length + 4, incoming_bytes);
        }
    }
    return 0;
disconnected:
    record("local transport disconnected; no request replay or fresh save replacement attempted");
    close_local_transport();
    return 0;
}

__declspec(dllexport) int offline_adapter_detach(void *lua_state) {
    (void)lua_state;
    if (attached_handler && GetCurrentThreadId() == owner_thread &&
        pointer_at(attached_handler, 0) == image + 0x832f4a0 &&
        pointer_at(attached_handler, 0x1f8) == (uintptr_t)local_connector) {
        memcpy((void *)(attached_handler + 0x1f8), &original_connector, sizeof(original_connector));
        record("detached local send alias; original shared ownership unchanged");
    }
    attached_handler = 0;
    close_local_transport();
    transport_mode = 0;
    DeleteFileA("offline-adapter-attached.txt");
    return 0;
}

__declspec(dllexport) int offline_adapter_capture(void *lua_state) {
    (void)lua_state;
    if (!guards_match() || attached_handler) return 0;
    uintptr_t manager = network_manager();
    uintptr_t handler = pointer_at(manager, 0x48);
    uintptr_t connector = pointer_at(handler, 0x1f8);
    if (!manager || pointer_at(handler, 0) != image + 0x832f4a0 ||
        pointer_at(connector, 0) != image + 0x832f248 ||
        pointer_at(connector, 8) != 0 || !readable((void *)connector, sizeof(local_connector)) ||
        connector != pointer_at(manager, 0x38) ||
        pointer_at(handler, 0x200) != pointer_at(manager, 0x40)) {
        record("ABSTAIN: offline network ownership or interface mismatch");
        return 0;
    }
    typedef void *(*getter)(void);
    typedef void *(*subsystem_getter)(void *, void *);
    struct native_string { const wchar_t *data; int num, capacity; };
    typedef void (*set_local_account)(void *, const struct native_string *);
    uintptr_t gi = (uintptr_t)((getter)(image + RVA_GAME_INSTANCE))();
    void *account_type = ((getter)(image + RVA_ACCOUNT_CLASS))();
    void *account = ((subsystem_getter)(image + RVA_GET_SUBSYSTEM))((void *)(gi + 0x100), account_type);
    if (!account || pointer_at((uintptr_t)account, 0x10) != (uintptr_t)account_type) return 0;
    const struct native_string offline_id = {L"10000001", 9, 9};
    ((set_local_account)(image + RVA_SET_LOCAL_ACCOUNT))(account, &offline_id);
    owner_thread = GetCurrentThreadId();
    FILETIME generation;
    GetSystemTimeAsFileTime(&generation);
    capture_generation = ((ULONGLONG)generation.dwHighDateTime << 32) | generation.dwLowDateTime;
    captured_requests = 0;
    for (size_t i = 0; i < sizeof(local_sdk_vtable) / sizeof(local_sdk_vtable[0]); ++i)
        local_sdk_vtable[i] = (uintptr_t)unsupported_slot;
    local_sdk_vtable[7] = (uintptr_t)capture_request;
    local_sdk_vtable[21] = (uintptr_t)local_connected;
    local_sdk = (uintptr_t)local_sdk_vtable;
    memcpy(local_connector, (void *)connector, sizeof(local_connector));
    uintptr_t sdk_address = (uintptr_t)&local_sdk;
    memcpy(local_connector + 8, &sdk_address, sizeof(sdk_address));
    original_connector = connector;
    uintptr_t alias = (uintptr_t)local_connector;
    memcpy((void *)(handler + 0x1f8), &alias, sizeof(alias));
    attached_handler = handler;
    FILE *ready = fopen("offline-adapter-attached.txt", "wb");
    if (ready) { fputs("capture-only\n", ready); fclose(ready); }
    record("attached capture-only original-message adapter on thread %lu", (unsigned long)owner_thread);
    return 0;
}

__declspec(dllexport) int offline_adapter_connect(void *lua_state) {
    if (attached_handler) return 0;
    DeleteFileA("offline-adapter-attached.txt");
    if (!guards_match() || attached_handler || !connect_loopback()) return 0;
    DeleteFileA("offline-native-action.txt");
    offline_adapter_capture(lua_state);
    if (!attached_handler) { close_local_transport(); return 0; }
    transport_mode = 1;
    record("native business messages routed to loopback endpoint; game interfaces retained");
    return 0;
}

__declspec(dllexport) int offline_adapter_probe(void *lua_state) {
    (void)lua_state;
    if (!guards_match()) return 0;
    typedef void *(*getter)(void);
    typedef void *(*subsystem_getter)(void *, void *);
    getter get_game_instance = (getter)(image + RVA_GAME_INSTANCE);
    getter get_network_class = (getter)(image + RVA_NETWORK_CLASS);
    subsystem_getter get_subsystem = (subsystem_getter)(image + RVA_GET_SUBSYSTEM);
    uintptr_t gi = (uintptr_t)get_game_instance();
    void *class_type = get_network_class();
    if (!gi || !readable((void *)(gi + 0x100), 0x30) || !class_type) {
        record("ABSTAIN: no live test game instance");
        return 0;
    }
    uintptr_t manager = (uintptr_t)get_subsystem((void *)(gi + 0x100), class_type);
    if (!manager || !readable((void *)manager, 0xa8)) {
        record("ABSTAIN: no live network subsystem");
        return 0;
    }
    if (pointer_at(manager, 0x10) != (uintptr_t)class_type) {
        record("ABSTAIN: subsystem is not the exact registered network class");
        return 0;
    }
    record("read-only adapter probe: image=%p gi=%p manager=%p", (void *)image, (void *)gi, (void *)manager);
    for (size_t offset = 0x30; offset <= 0x98; offset += 8) {
        uintptr_t p = pointer_at(manager, offset);
        record("manager+%02zx=%p vtable=%p", offset, (void *)p, (void *)pointer_at(p, 0));
    }
    uintptr_t handler = pointer_at(manager, 0x48);
    uintptr_t connector = pointer_at(handler, 0x1f8);
    uintptr_t sdk = pointer_at(connector, 8);
    record("candidate chain: handler=%p connector=%p sdk=%p sdk-vtable=%p",
           (void *)handler, (void *)connector, (void *)sdk, (void *)pointer_at(sdk, 0));
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);
    return TRUE;
}
