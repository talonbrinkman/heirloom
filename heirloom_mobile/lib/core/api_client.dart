import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'storage/device_storage.dart';

/// SharedPreferences instance overridden in `main.dart`.
final sharedPreferencesProvider = Provider<SharedPreferences>((ref) {
  throw UnimplementedError('sharedPreferencesProvider must be overridden in main.dart');
});

/// Optional override for SecureKeyValueStore (overridden in unit tests).
final secureKeyValueStoreProvider = Provider<SecureKeyValueStore>((ref) {
  return FlutterSecureKeyValueStore();
});

/// DeviceStorage manages saved servers + encrypted credentials on device.
final deviceStorageProvider = Provider<DeviceStorage>((ref) {
  final prefs = ref.watch(sharedPreferencesProvider);
  final secure = ref.watch(secureKeyValueStoreProvider);
  return DeviceStorage(prefs, secure);
});

class AppSessionState {
  final bool isInitialized;
  final List<SavedServer> savedServers;
  final SavedServer? activeServer;
  final String? authToken;
  final int? activeProfileId;
  final bool rememberMe;
  final Map<String, dynamic>? serverInfo;
  final Map<int, String> pinUnlockTokens;

  const AppSessionState({
    this.isInitialized = false,
    this.savedServers = const [],
    this.activeServer,
    this.authToken,
    this.activeProfileId,
    this.rememberMe = true,
    this.serverInfo,
    this.pinUnlockTokens = const {},
  });

  String? get serverUrl => activeServer?.url;
  bool get isSetup => serverUrl != null && serverUrl!.isNotEmpty;
  bool get isAuthenticated => authToken != null && authToken!.isNotEmpty;

  Map<String, dynamic> get features {
    final raw = serverInfo?['features'];
    if (raw is Map) return Map<String, dynamic>.from(raw);
    return const {'movies': true, 'tv': true, 'photos': true, 'music': true};
  }

  Map<String, dynamic> get userInfo {
    final raw = serverInfo?['user'];
    if (raw is Map) return Map<String, dynamic>.from(raw);
    return const {};
  }

  bool get isAdmin => userInfo['is_admin'] == true;

  AppSessionState copyWith({
    bool? isInitialized,
    List<SavedServer>? savedServers,
    SavedServer? activeServer,
    bool clearActiveServer = false,
    String? authToken,
    bool clearAuthToken = false,
    int? activeProfileId,
    bool clearActiveProfileId = false,
    bool? rememberMe,
    Map<String, dynamic>? serverInfo,
    bool clearServerInfo = false,
    Map<int, String>? pinUnlockTokens,
  }) {
    return AppSessionState(
      isInitialized: isInitialized ?? this.isInitialized,
      savedServers: savedServers ?? this.savedServers,
      activeServer: clearActiveServer ? null : (activeServer ?? this.activeServer),
      authToken: clearAuthToken ? null : (authToken ?? this.authToken),
      activeProfileId: clearActiveProfileId ? null : (activeProfileId ?? this.activeProfileId),
      rememberMe: rememberMe ?? this.rememberMe,
      serverInfo: clearServerInfo ? null : (serverInfo ?? this.serverInfo),
      pinUnlockTokens: pinUnlockTokens ?? this.pinUnlockTokens,
    );
  }
}

class AppSessionNotifier extends StateNotifier<AppSessionState> {
  final DeviceStorage _storage;
  bool _handling401 = false;

  AppSessionNotifier(this._storage) : super(const AppSessionState());

  Future<void> init({bool refreshRemote = true}) async {
    await _storage.migrateLegacyIfNeeded();
    final servers = _storage.loadServers();
    final activeId = _storage.getActiveServerId();
    SavedServer? active = _storage.getServerById(activeId);
    if (active == null && servers.isNotEmpty) {
      active = servers.first;
      await _storage.setActiveServerId(active.id);
    }

    String? token;
    int? profileId;
    final pinTokens = <int, String>{};
    if (active != null) {
      token = await _storage.getToken(active.id);
      if (token != null && token.isNotEmpty) {
        profileId = active.lastProfileId;
        if (profileId != null) {
          final pt = await _storage.getPinUnlockToken(active.id, profileId);
          if (pt != null && pt.isNotEmpty) {
            pinTokens[profileId] = pt;
          }
        }
      }
    }

    state = state.copyWith(
      isInitialized: true,
      savedServers: servers,
      activeServer: active,
      authToken: token,
      activeProfileId: profileId,
      rememberMe: active?.rememberMe ?? true,
      pinUnlockTokens: pinTokens,
    );

    if (refreshRemote && active != null) {
      await refreshServerInfo(clearTokenOn401: true);
    }
  }

  Dio _plainDio(String baseUrl) {
    return Dio(
      BaseOptions(
        baseUrl: baseUrl,
        connectTimeout: const Duration(seconds: 10),
        receiveTimeout: const Duration(seconds: 15),
        headers: const {
          'Accept': 'application/json',
          'Content-Type': 'application/json',
        },
      ),
    );
  }

  /// Validates a server URL via `/api/info`, saves it on device, and sets it active.
  Future<SavedServer> connectAndSaveServer(String rawUrl) async {
    final normalized = DeviceStorage.normalizeUrl(rawUrl);
    if (normalized.isEmpty) {
      throw ArgumentError('Please enter a server URL');
    }

    final dio = _plainDio(normalized);
    final resp = await dio.get('/api/info');
    if (resp.statusCode != 200 || resp.data is! Map) {
      throw Exception('Invalid response from Heirloom server.');
    }

    final info = Map<String, dynamic>.from(resp.data as Map);
    final existingId = DeviceStorage.serverIdForUrl(normalized);
    final existing = _storage.getServerById(existingId);

    final server = await _storage.saveServer(
      SavedServer(
        id: existingId,
        name: (info['server_name'] as String?)?.isNotEmpty == true
            ? info['server_name'] as String
            : (Uri.tryParse(normalized)?.host ?? 'Heirloom'),
        url: normalized,
        version: (info['version'] as String?) ?? '2.6.6',
        lastUsername: existing?.lastUsername,
        lastProfileId: existing?.lastProfileId,
        rememberMe: existing?.rememberMe ?? true,
        lastUsedAt: DateTime.now().toUtc(),
      ),
    );

    await _storage.setActiveServerId(server.id);
    final token = await _storage.getToken(server.id);
    final pinTokens = <int, String>{};
    if (token != null && token.isNotEmpty && server.lastProfileId != null) {
      final pt = await _storage.getPinUnlockToken(server.id, server.lastProfileId!);
      if (pt != null && pt.isNotEmpty) {
        pinTokens[server.lastProfileId!] = pt;
      }
    }

    state = state.copyWith(
      isInitialized: true,
      savedServers: _storage.loadServers(),
      activeServer: server,
      authToken: token,
      clearAuthToken: token == null || token.isEmpty,
      activeProfileId: (token != null && token.isNotEmpty) ? server.lastProfileId : null,
      clearActiveProfileId: token == null || token.isEmpty || server.lastProfileId == null,
      rememberMe: server.rememberMe,
      serverInfo: info,
      pinUnlockTokens: pinTokens,
    );

    if (token != null && token.isNotEmpty) {
      await refreshServerInfo(clearTokenOn401: true);
    }
    return server;
  }

  /// Switches to a previously saved server on this device.
  Future<void> switchServer(SavedServer target) async {
    final updated = await _storage.saveServer(target.copyWith(lastUsedAt: DateTime.now().toUtc()));
    await _storage.setActiveServerId(updated.id);
    final token = await _storage.getToken(updated.id);
    final pinTokens = <int, String>{};
    if (token != null && token.isNotEmpty && updated.lastProfileId != null) {
      final pt = await _storage.getPinUnlockToken(updated.id, updated.lastProfileId!);
      if (pt != null && pt.isNotEmpty) {
        pinTokens[updated.lastProfileId!] = pt;
      }
    }

    state = state.copyWith(
      savedServers: _storage.loadServers(),
      activeServer: updated,
      authToken: token,
      clearAuthToken: token == null || token.isEmpty,
      activeProfileId: (token != null && token.isNotEmpty) ? updated.lastProfileId : null,
      clearActiveProfileId: token == null || token.isEmpty || updated.lastProfileId == null,
      rememberMe: updated.rememberMe,
      clearServerInfo: true,
      pinUnlockTokens: pinTokens,
    );

    await refreshServerInfo(clearTokenOn401: true);
  }

  Future<void> removeServer(String serverId) async {
    await _storage.removeServer(serverId);
    final remaining = _storage.loadServers();
    final newActiveId = _storage.getActiveServerId();
    final newActive = _storage.getServerById(newActiveId);
    String? token;
    int? profileId;
    if (newActive != null) {
      token = await _storage.getToken(newActive.id);
      if (token != null && token.isNotEmpty) {
        profileId = newActive.lastProfileId;
      }
    }
    state = state.copyWith(
      savedServers: remaining,
      activeServer: newActive,
      clearActiveServer: newActive == null,
      authToken: token,
      clearAuthToken: token == null || token.isEmpty,
      activeProfileId: profileId,
      clearActiveProfileId: profileId == null,
      clearServerInfo: true,
    );
  }

  Future<String?> getSavedPasswordForActiveServer() async {
    final active = state.activeServer;
    if (active == null) return null;
    return _storage.getSavedPassword(active.id);
  }

  Future<void> refreshServerInfo({bool clearTokenOn401 = false}) async {
    final active = state.activeServer;
    if (active == null) return;
    try {
      final dio = _plainDio(active.url);
      final headers = <String, dynamic>{};
      if (state.authToken != null && state.authToken!.isNotEmpty) {
        headers['Authorization'] = 'Bearer ${state.authToken}';
      }
      if (state.activeProfileId != null) {
        headers['X-Profile-ID'] = state.activeProfileId.toString();
      }
      final resp = await dio.get('/api/info', options: Options(headers: headers));
      if (resp.statusCode == 200 && resp.data is Map) {
        final info = Map<String, dynamic>.from(resp.data as Map);
        final authenticated = info['authenticated'] == true;
        final saved = await _storage.saveServer(
          active.copyWith(
            name: (info['server_name'] as String?)?.isNotEmpty == true ? info['server_name'] as String : active.name,
            version: (info['version'] as String?) ?? active.version,
          ),
        );

        if (clearTokenOn401 && state.authToken != null && !authenticated) {
          await _storage.setToken(active.id, null);
          state = state.copyWith(
            savedServers: _storage.loadServers(),
            activeServer: saved,
            clearAuthToken: true,
            clearActiveProfileId: true,
            serverInfo: info,
          );
          return;
        }

        state = state.copyWith(
          savedServers: _storage.loadServers(),
          activeServer: saved,
          serverInfo: info,
        );
      }
    } on DioException catch (e) {
      if (clearTokenOn401 && e.response?.statusCode == 401) {
        await _storage.setToken(active.id, null);
        state = state.copyWith(
          clearAuthToken: true,
          clearActiveProfileId: true,
        );
      }
    } catch (_) {
      // Offline/transient failure: keep cached state so user isn't kicked out unnecessarily
    }
  }

  /// Authenticates via `/api/login` using Heirloom's `remember_me` behavior (default enabled).
  Future<void> login(String username, String password, {bool rememberMe = true}) async {
    final active = state.activeServer;
    if (active == null) {
      throw StateError('No active server configured.');
    }

    final dio = _plainDio(active.url);
    final Response<dynamic> resp;
    try {
      resp = await dio.post('/api/login', data: {
        'username': username,
        'password': password,
        'remember_me': rememberMe,
      });
    } on DioException catch (e) {
      throw Exception(formatApiError(e, isLogin: true));
    }

    if (resp.statusCode != 200 || resp.data is! Map || resp.data['success'] != true) {
      final err = (resp.data is Map ? resp.data['error'] : null) ?? 'Invalid username or password.';
      throw Exception(err.toString());
    }

    final token = resp.data['token'] as String;
    if (rememberMe) {
      await _storage.setToken(active.id, token);
      await _storage.setSavedPassword(active.id, password);
    } else {
      await _storage.setToken(active.id, null);
      await _storage.setSavedPassword(active.id, null);
    }

    // Check profiles so single PIN-less profile selects automatically (matching web login.html)
    int? autoProfileId;
    try {
      final profResp = await dio.get(
        '/api/profiles/list',
        options: Options(headers: {'Authorization': 'Bearer $token'}),
      );
      if (profResp.statusCode == 200 && profResp.data is List) {
        final list = profResp.data as List;
        if (list.length == 1 && list.first is Map) {
          final only = Map<String, dynamic>.from(list.first as Map);
          if (only['has_pin'] != true && only['id'] != null) {
            final pid = only['id'] as int;
            await dio.post(
              '/api/profile/select',
              data: {'profile_id': pid},
              options: Options(headers: {'Authorization': 'Bearer $token'}),
            );
            autoProfileId = pid;
          }
        }
      }
    } catch (_) {}

    final updatedServer = await _storage.saveServer(
      active.copyWith(
        lastUsername: rememberMe ? username : null,
        clearLastUsername: !rememberMe,
        lastProfileId: rememberMe ? autoProfileId : null,
        clearLastProfileId: !rememberMe || autoProfileId == null,
        rememberMe: rememberMe,
        lastUsedAt: DateTime.now().toUtc(),
      ),
    );

    state = state.copyWith(
      savedServers: _storage.loadServers(),
      activeServer: updatedServer,
      authToken: token,
      activeProfileId: autoProfileId,
      clearActiveProfileId: autoProfileId == null,
      rememberMe: rememberMe,
    );

    await refreshServerInfo(clearTokenOn401: false);
  }

  Future<void> selectProfile(int profileId, {String? pinUnlockToken}) async {
    final active = state.activeServer;
    final nextPins = Map<int, String>.from(state.pinUnlockTokens);
    if (pinUnlockToken != null && pinUnlockToken.isNotEmpty) {
      nextPins[profileId] = pinUnlockToken;
    }

    SavedServer? updatedServer = active;
    if (active != null) {
      if (state.rememberMe) {
        if (pinUnlockToken != null && pinUnlockToken.isNotEmpty) {
          await _storage.setPinUnlockToken(active.id, profileId, pinUnlockToken);
        }
        updatedServer = await _storage.saveServer(
          active.copyWith(lastProfileId: profileId, lastUsedAt: DateTime.now().toUtc()),
        );
      }
    }

    state = state.copyWith(
      savedServers: _storage.loadServers(),
      activeServer: updatedServer,
      activeProfileId: profileId,
      pinUnlockTokens: nextPins,
    );

    await refreshServerInfo(clearTokenOn401: true);
  }

  /// Calls `POST /logout` on the backend (revoking token) and clears local session token.
  Future<void> signOut({bool clearSavedPassword = false}) async {
    final active = state.activeServer;
    final token = state.authToken;

    if (active != null && token != null && token.isNotEmpty) {
      try {
        await _plainDio(active.url).post(
          '/logout',
          options: Options(headers: {'Authorization': 'Bearer $token', 'Accept': 'application/json'}),
        );
      } catch (_) {}
    }

    if (active != null) {
      await _storage.setToken(active.id, null);
      if (clearSavedPassword) {
        await _storage.clearServerCredentials(active.id, keepUsername: false);
      } else {
        await _storage.saveServer(active.copyWith(clearLastProfileId: true));
      }
    }

    state = state.copyWith(
      savedServers: _storage.loadServers(),
      activeServer: active != null ? _storage.getServerById(active.id) : null,
      clearAuthToken: true,
      clearActiveProfileId: true,
      clearServerInfo: true,
      pinUnlockTokens: const {},
    );
  }

  Future<void> clearAllSavedCredentials() async {
    final active = state.activeServer;
    if (active != null) {
      await _storage.clearServerCredentials(active.id, keepUsername: false);
    }
    await signOut(clearSavedPassword: true);
  }

  Future<void> handleUnauthorized() async {
    if (_handling401 || state.authToken == null) return;
    _handling401 = true;
    try {
      final active = state.activeServer;
      if (active != null) {
        await _storage.setToken(active.id, null);
      }
      state = state.copyWith(
        clearAuthToken: true,
        clearActiveProfileId: true,
        clearServerInfo: true,
      );
    } finally {
      _handling401 = false;
    }
  }
}

final appSessionProvider = StateNotifierProvider<AppSessionNotifier, AppSessionState>((ref) {
  final storage = ref.watch(deviceStorageProvider);
  return AppSessionNotifier(storage);
});

// Derived compatibility providers watched across existing screens and GoRouter
final serverUrlProvider = Provider<String?>((ref) {
  return ref.watch(appSessionProvider.select((s) => s.serverUrl));
});

final authTokenProvider = Provider<String?>((ref) {
  return ref.watch(appSessionProvider.select((s) => s.authToken));
});

final activeProfileIdProvider = Provider<int?>((ref) {
  return ref.watch(appSessionProvider.select((s) => s.activeProfileId));
});

/// Active Profile details provider
final activeProfileInfoProvider = FutureProvider<Map<String, dynamic>?>((ref) async {
  final activeId = ref.watch(activeProfileIdProvider);
  final authToken = ref.watch(authTokenProvider);
  if (authToken == null || authToken.isEmpty || activeId == null) return null;

  try {
    final dio = ref.watch(apiClientProvider);
    final resp = await dio.get('/api/profiles/list');
    final list = resp.data as List<dynamic>;
    for (final item in list) {
      if (item is Map && item['id'] == activeId) {
        return Map<String, dynamic>.from(item);
      }
    }
  } catch (_) {}
  return null;
});

/// Authenticated Dio API client automatically injecting Bearer token, Profile ID, and PIN unlock headers.
final apiClientProvider = Provider<Dio>((ref) {
  final session = ref.watch(appSessionProvider);

  final dio = Dio(
    BaseOptions(
      baseUrl: session.serverUrl ?? '',
      connectTimeout: const Duration(seconds: 10),
      receiveTimeout: const Duration(seconds: 20),
      headers: const {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      },
    ),
  );

  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        final token = session.authToken;
        if (token != null && token.isNotEmpty) {
          options.headers['Authorization'] = 'Bearer $token';
        }
        final pid = session.activeProfileId;
        if (pid != null) {
          options.headers['X-Profile-ID'] = pid.toString();
          final unlockToken = session.pinUnlockTokens[pid];
          if (unlockToken != null && unlockToken.isNotEmpty) {
            options.headers['X-Profile-Unlock-$pid'] = unlockToken;
          }
        }
        return handler.next(options);
      },
      onError: (DioException e, handler) {
        if (e.response?.statusCode == 401) {
          final path = e.requestOptions.path;
          if (!path.contains('/api/login') && !path.contains('/api/info')) {
            ref.read(appSessionProvider.notifier).handleUnauthorized();
          }
        }
        return handler.next(e);
      },
    ),
  );

  return dio;
});

String? buildMetadataImageUrl(String? serverUrl, String? filename) {
  if (serverUrl == null || serverUrl.isEmpty || filename == null || filename.isEmpty) {
    return null;
  }
  if (filename.startsWith('http://') || filename.startsWith('https://')) {
    return filename;
  }
  if (filename.startsWith('/')) {
    return '$serverUrl$filename';
  }
  return '$serverUrl/metadata/$filename';
}

/// Converts raw `DioException` / backend errors into clean, user-friendly messages.
String formatApiError(Object error, {String? fallback, bool isLogin = false}) {
  if (error is DioException) {
    final data = error.response?.data;
    if (data is Map) {
      for (final key in const ['error', 'detail', 'message']) {
        final val = data[key];
        if (val != null && val.toString().trim().isNotEmpty) {
          return val.toString().trim();
        }
      }
    }
    final status = error.response?.statusCode;
    if (status == 401) {
      return isLogin
          ? 'Invalid username or password.'
          : 'Your session expired. Please sign in again.';
    }
    if (status == 403) return 'You do not have permission to perform this action.';
    if (status == 404) return 'The requested item could not be found.';
    if (status == 429) return 'Too many attempts. Please wait a moment and try again.';
    if (status != null && status >= 500) {
      return 'Server error ($status). Please try again shortly.';
    }
    switch (error.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.sendTimeout:
      case DioExceptionType.receiveTimeout:
      case DioExceptionType.connectionError:
        return 'Unable to reach the server. Check your connection and server URL.';
      default:
        break;
    }
    return fallback ?? 'Request failed. Please try again.';
  }

  final raw = error.toString().trim();
  if (raw.startsWith('Exception: ')) return raw.substring('Exception: '.length);
  if (raw.startsWith('Bad state: ')) return raw.substring('Bad state: '.length);
  return raw.isNotEmpty ? raw : (fallback ?? 'Something went wrong.');
}
