import 'dart:convert';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Abstraction for encrypted key-value storage so unit tests can run without platform channels.
abstract class SecureKeyValueStore {
  Future<String?> read(String key);
  Future<void> write(String key, String value);
  Future<void> delete(String key);
}

class FlutterSecureKeyValueStore implements SecureKeyValueStore {
  final FlutterSecureStorage _storage;
  final Map<String, String> _memoryFallback = {};

  FlutterSecureKeyValueStore([FlutterSecureStorage? storage])
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
            );

  @override
  Future<String?> read(String key) async {
    try {
      return await _storage.read(key: key);
    } catch (_) {
      return _memoryFallback[key];
    }
  }

  @override
  Future<void> write(String key, String value) async {
    try {
      await _storage.write(key: key, value: value);
    } catch (_) {
      _memoryFallback[key] = value;
    }
  }

  @override
  Future<void> delete(String key) async {
    try {
      await _storage.delete(key: key);
    } catch (_) {
      _memoryFallback.remove(key);
    }
  }
}

class InMemorySecureKeyValueStore implements SecureKeyValueStore {
  final Map<String, String> _data = {};

  @override
  Future<String?> read(String key) async => _data[key];

  @override
  Future<void> write(String key, String value) async {
    _data[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    _data.remove(key);
  }
}

/// Persistent representation of a saved Heirloom server + last login metadata.
class SavedServer {
  final String id;
  final String name;
  final String url;
  final String version;
  final String? lastUsername;
  final int? lastProfileId;
  final bool rememberMe;
  final DateTime lastUsedAt;

  const SavedServer({
    required this.id,
    required this.name,
    required this.url,
    this.version = '2.6.6',
    this.lastUsername,
    this.lastProfileId,
    this.rememberMe = true, // Matches web login "Keep me signed in" default checked
    required this.lastUsedAt,
  });

  SavedServer copyWith({
    String? id,
    String? name,
    String? url,
    String? version,
    String? lastUsername,
    bool clearLastUsername = false,
    int? lastProfileId,
    bool clearLastProfileId = false,
    bool? rememberMe,
    DateTime? lastUsedAt,
  }) {
    return SavedServer(
      id: id ?? this.id,
      name: name ?? this.name,
      url: url ?? this.url,
      version: version ?? this.version,
      lastUsername: clearLastUsername ? null : (lastUsername ?? this.lastUsername),
      lastProfileId: clearLastProfileId ? null : (lastProfileId ?? this.lastProfileId),
      rememberMe: rememberMe ?? this.rememberMe,
      lastUsedAt: lastUsedAt ?? this.lastUsedAt,
    );
  }

  Map<String, dynamic> toJson() => {
        'id': id,
        'name': name,
        'url': url,
        'version': version,
        'last_username': lastUsername,
        'last_profile_id': lastProfileId,
        'remember_me': rememberMe,
        'last_used_at': lastUsedAt.toUtc().toIso8601String(),
      };

  factory SavedServer.fromJson(Map<String, dynamic> json) {
    final url = DeviceStorage.normalizeUrl(json['url'] as String? ?? '');
    return SavedServer(
      id: (json['id'] as String?)?.isNotEmpty == true ? json['id'] as String : DeviceStorage.serverIdForUrl(url),
      name: (json['name'] as String?)?.isNotEmpty == true ? json['name'] as String : 'Heirloom',
      url: url,
      version: (json['version'] as String?) ?? '2.6.6',
      lastUsername: json['last_username'] as String?,
      lastProfileId: json['last_profile_id'] is int
          ? json['last_profile_id'] as int
          : int.tryParse('${json['last_profile_id']}'),
      rememberMe: json['remember_me'] is bool ? json['remember_me'] as bool : true,
      lastUsedAt: DateTime.tryParse(json['last_used_at'] as String? ?? '') ?? DateTime.now().toUtc(),
    );
  }
}

/// Manages device-saved server connections and encrypted login credentials.
class DeviceStorage {
  static const String serversPrefsKey = 'saved_servers_v1';
  static const String activeServerIdKey = 'active_server_id_v1';
  static const String migratedFlagKey = 'legacy_storage_migrated_v1';

  // Legacy keys from older mobile builds
  static const String _legacyServerUrlKey = 'server_url';
  static const String _legacyAuthTokenKey = 'auth_token';
  static const String _legacyProfileIdKey = 'active_profile_id';
  static const String _legacyUnlockPrefix = 'profile_unlocked_';

  final SharedPreferences _prefs;
  final SecureKeyValueStore _secure;

  DeviceStorage(this._prefs, [SecureKeyValueStore? secureStore])
      : _secure = secureStore ?? FlutterSecureKeyValueStore();

  static String normalizeUrl(String rawUrl) {
    var trimmed = rawUrl.trim();
    if (trimmed.isEmpty) return '';
    if (!trimmed.startsWith('http://') && !trimmed.startsWith('https://')) {
      trimmed = 'http://$trimmed';
    }
    while (trimmed.endsWith('/')) {
      trimmed = trimmed.substring(0, trimmed.length - 1);
    }
    return trimmed;
  }

  static String serverIdForUrl(String url) {
    final normalized = normalizeUrl(url).toLowerCase();
    return base64Url.encode(utf8.encode(normalized)).replaceAll('=', '');
  }

  String _tokenKey(String serverId) => 'token_$serverId';
  String _passwordKey(String serverId) => 'password_$serverId';
  String _pinUnlockKey(String serverId, int profileId) => 'pin_unlock_${serverId}_$profileId';
  String _pinRegistryKey(String serverId) => 'pin_unlock_ids_$serverId';

  /// One-time migration from plaintext SharedPreferences (`server_url`, `auth_token`, `active_profile_id`).
  Future<void> migrateLegacyIfNeeded() async {
    if (_prefs.getBool(migratedFlagKey) == true && _prefs.containsKey(serversPrefsKey)) {
      return;
    }

    final legacyUrl = _prefs.getString(_legacyServerUrlKey);
    final legacyToken = _prefs.getString(_legacyAuthTokenKey);
    final legacyProfileId = _prefs.getInt(_legacyProfileIdKey);

    if (legacyUrl != null && legacyUrl.trim().isNotEmpty) {
      final normalized = normalizeUrl(legacyUrl);
      final serverId = serverIdForUrl(normalized);
      final servers = loadServers();
      if (!servers.any((s) => s.id == serverId)) {
        servers.add(SavedServer(
          id: serverId,
          name: Uri.tryParse(normalized)?.host ?? 'Heirloom',
          url: normalized,
          lastProfileId: legacyProfileId,
          rememberMe: true,
          lastUsedAt: DateTime.now().toUtc(),
        ));
        await _saveServersList(servers);
      }
      await setActiveServerId(serverId);

      if (legacyToken != null && legacyToken.isNotEmpty) {
        await setToken(serverId, legacyToken);
      }

      // Migrate any legacy PIN unlock tokens
      final keysToRemove = <String>[];
      for (final k in _prefs.getKeys()) {
        if (k.startsWith(_legacyUnlockPrefix)) {
          final pid = int.tryParse(k.substring(_legacyUnlockPrefix.length));
          final val = _prefs.getString(k);
          if (pid != null && val != null && val.isNotEmpty) {
            await setPinUnlockToken(serverId, pid, val);
          }
          keysToRemove.add(k);
        }
      }
      for (final k in keysToRemove) {
        await _prefs.remove(k);
      }
    }

    // Remove sensitive legacy plaintext token & old single-server keys
    await _prefs.remove(_legacyAuthTokenKey);
    await _prefs.remove(_legacyServerUrlKey);
    await _prefs.remove(_legacyProfileIdKey);
    await _prefs.setBool(migratedFlagKey, true);
  }

  List<SavedServer> loadServers() {
    final raw = _prefs.getString(serversPrefsKey);
    if (raw == null || raw.isEmpty) return [];
    try {
      final decoded = jsonDecode(raw);
      if (decoded is! List) return [];
      final list = decoded
          .whereType<Map>()
          .map((m) => SavedServer.fromJson(Map<String, dynamic>.from(m)))
          .where((s) => s.url.isNotEmpty)
          .toList();
      list.sort((a, b) => b.lastUsedAt.compareTo(a.lastUsedAt));
      return list;
    } catch (_) {
      return [];
    }
  }

  Future<void> _saveServersList(List<SavedServer> servers) async {
    final encoded = jsonEncode(servers.map((s) => s.toJson()).toList());
    await _prefs.setString(serversPrefsKey, encoded);
  }

  SavedServer? getServerById(String? serverId) {
    if (serverId == null || serverId.isEmpty) return null;
    for (final s in loadServers()) {
      if (s.id == serverId) return s;
    }
    return null;
  }

  String? getActiveServerId() => _prefs.getString(activeServerIdKey);

  Future<void> setActiveServerId(String? serverId) async {
    if (serverId == null || serverId.isEmpty) {
      await _prefs.remove(activeServerIdKey);
    } else {
      await _prefs.setString(activeServerIdKey, serverId);
    }
  }

  Future<SavedServer> saveServer(SavedServer server) async {
    final normalized = normalizeUrl(server.url);
    final updated = server.copyWith(
      id: serverIdForUrl(normalized),
      url: normalized,
      lastUsedAt: DateTime.now().toUtc(),
    );
    final servers = loadServers();
    final idx = servers.indexWhere((s) => s.id == updated.id || s.url.toLowerCase() == updated.url.toLowerCase());
    if (idx >= 0) {
      servers[idx] = updated;
    } else {
      servers.add(updated);
    }
    await _saveServersList(servers);
    return updated;
  }

  Future<void> removeServer(String serverId) async {
    await clearServerCredentials(serverId);
    final servers = loadServers()..removeWhere((s) => s.id == serverId);
    await _saveServersList(servers);
    if (getActiveServerId() == serverId) {
      await setActiveServerId(servers.isNotEmpty ? servers.first.id : null);
    }
  }

  Future<String?> getToken(String serverId) => _secure.read(_tokenKey(serverId));

  Future<void> setToken(String serverId, String? token) async {
    if (token == null || token.isEmpty) {
      await _secure.delete(_tokenKey(serverId));
    } else {
      await _secure.write(_tokenKey(serverId), token);
    }
  }

  Future<String?> getSavedPassword(String serverId) => _secure.read(_passwordKey(serverId));

  Future<void> setSavedPassword(String serverId, String? password) async {
    if (password == null || password.isEmpty) {
      await _secure.delete(_passwordKey(serverId));
    } else {
      await _secure.write(_passwordKey(serverId), password);
    }
  }

  Future<String?> getPinUnlockToken(String serverId, int profileId) =>
      _secure.read(_pinUnlockKey(serverId, profileId));

  Future<void> setPinUnlockToken(String serverId, int profileId, String? token) async {
    final tracked = Set<String>.from(_prefs.getStringList(_pinRegistryKey(serverId)) ?? const []);
    if (token == null || token.isEmpty) {
      await _secure.delete(_pinUnlockKey(serverId, profileId));
      tracked.remove(profileId.toString());
    } else {
      await _secure.write(_pinUnlockKey(serverId, profileId), token);
      tracked.add(profileId.toString());
    }
    await _prefs.setStringList(_pinRegistryKey(serverId), tracked.toList());
  }

  Future<void> clearServerCredentials(String serverId, {bool keepUsername = false}) async {
    await _secure.delete(_tokenKey(serverId));
    await _secure.delete(_passwordKey(serverId));
    final tracked = _prefs.getStringList(_pinRegistryKey(serverId)) ?? const [];
    for (final pidStr in tracked) {
      final pid = int.tryParse(pidStr);
      if (pid != null) {
        await _secure.delete(_pinUnlockKey(serverId, pid));
      }
    }
    await _prefs.remove(_pinRegistryKey(serverId));

    final existing = getServerById(serverId);
    if (existing != null) {
      await saveServer(existing.copyWith(
        clearLastProfileId: true,
        clearLastUsername: !keepUsername,
      ));
    }
  }

  Future<void> clearAll() async {
    for (final s in loadServers()) {
      await clearServerCredentials(s.id);
    }
    await _prefs.remove(serversPrefsKey);
    await _prefs.remove(activeServerIdKey);
  }
}
