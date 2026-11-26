import 'package:flutter_test/flutter_test.dart';
import 'package:heirloom_mobile/core/storage/device_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  group('DeviceStorage', () {
    setUp(() {
      SharedPreferences.setMockInitialValues({});
    });

    test('SavedServer defaults rememberMe to true and normalizes URLs', () async {
      final prefs = await SharedPreferences.getInstance();
      final storage = DeviceStorage(prefs, InMemorySecureKeyValueStore());

      final saved = await storage.saveServer(
        SavedServer(
          id: '',
          name: 'Home',
          url: '192.168.1.50:42069/',
          lastUsedAt: DateTime.now().toUtc(),
        ),
      );

      expect(saved.url, 'http://192.168.1.50:42069');
      expect(saved.rememberMe, isTrue);

      final loaded = storage.loadServers();
      expect(loaded.length, 1);
      expect(loaded.first.url, 'http://192.168.1.50:42069');
      expect(loaded.first.rememberMe, isTrue);
    });

    test('Migrates legacy plaintext token & URL into secure storage', () async {
      SharedPreferences.setMockInitialValues({
        'server_url': 'https://media.example.com/',
        'auth_token': 'legacy-secret-token',
        'active_profile_id': 7,
        'profile_unlocked_7': 'pin-unlock-abc',
      });
      final prefs = await SharedPreferences.getInstance();
      final secure = InMemorySecureKeyValueStore();
      final storage = DeviceStorage(prefs, secure);

      await storage.migrateLegacyIfNeeded();

      expect(prefs.getString('auth_token'), isNull);
      expect(prefs.getString('server_url'), isNull);
      expect(prefs.getInt('active_profile_id'), isNull);

      final servers = storage.loadServers();
      expect(servers.length, 1);
      final srv = servers.first;
      expect(srv.url, 'https://media.example.com');
      expect(srv.lastProfileId, 7);
      expect(srv.rememberMe, isTrue);

      expect(await storage.getToken(srv.id), 'legacy-secret-token');
      expect(await storage.getPinUnlockToken(srv.id, 7), 'pin-unlock-abc');
    });

    test('clearServerCredentials clears secure token, password, and PIN unlock tokens', () async {
      final prefs = await SharedPreferences.getInstance();
      final secure = InMemorySecureKeyValueStore();
      final storage = DeviceStorage(prefs, secure);

      final srv = await storage.saveServer(
        SavedServer(
          id: 's1',
          name: 'Living Room',
          url: 'http://localhost:42069',
          lastUsername: 'alice',
          lastProfileId: 2,
          lastUsedAt: DateTime.now().toUtc(),
        ),
      );
      await storage.setToken(srv.id, 'tok-1');
      await storage.setSavedPassword(srv.id, 'super-secret');
      await storage.setPinUnlockToken(srv.id, 2, 'unlock-2');

      await storage.clearServerCredentials(srv.id);

      expect(await storage.getToken(srv.id), isNull);
      expect(await storage.getSavedPassword(srv.id), isNull);
      expect(await storage.getPinUnlockToken(srv.id, 2), isNull);

      final updated = storage.getServerById(srv.id);
      expect(updated?.lastUsername, isNull);
      expect(updated?.lastProfileId, isNull);
    });
  });
}
