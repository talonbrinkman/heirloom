import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

// Providers for dependencies
final sharedPreferencesProvider = Provider<SharedPreferences>((ref) {
  throw UnimplementedError('sharedPreferencesProvider must be overridden in main.dart');
});

// State providers for our server URL and token
final serverUrlProvider = StateProvider<String?>((ref) {
  final prefs = ref.watch(sharedPreferencesProvider);
  return prefs.getString('server_url');
});

final authTokenProvider = StateProvider<String?>((ref) {
  final prefs = ref.watch(sharedPreferencesProvider);
  return prefs.getString('auth_token');
});

final activeProfileIdProvider = StateProvider<int?>((ref) {
  final prefs = ref.watch(sharedPreferencesProvider);
  return prefs.getInt('active_profile_id');
});

// Active Profile details provider
final activeProfileInfoProvider = FutureProvider<Map<String, dynamic>?>((ref) async {
  final activeId = ref.watch(activeProfileIdProvider);
  final authToken = ref.watch(authTokenProvider);
  if (authToken == null || authToken.isEmpty) return null;
  if (activeId == null) return null;

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

// Dio API Client Provider
final apiClientProvider = Provider<Dio>((ref) {
  final serverUrl = ref.watch(serverUrlProvider);
  final authToken = ref.watch(authTokenProvider);
  final activeProfileId = ref.watch(activeProfileIdProvider);
  final prefs = ref.watch(sharedPreferencesProvider);

  final dio = Dio(
    BaseOptions(
      baseUrl: serverUrl ?? '',
      connectTimeout: const Duration(seconds: 10),
      receiveTimeout: const Duration(seconds: 10),
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      },
    ),
  );

  // Add an interceptor to automatically inject the Bearer token, active profile ID, and unlock token
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        if (authToken != null && authToken.isNotEmpty) {
          options.headers['Authorization'] = 'Bearer $authToken';
        }
        if (activeProfileId != null) {
          options.headers['X-Profile-ID'] = activeProfileId.toString();
          final unlockToken = prefs.getString('profile_unlocked_$activeProfileId');
          if (unlockToken != null && unlockToken.isNotEmpty) {
            options.headers['X-Profile-Unlock-$activeProfileId'] = unlockToken;
          }
        }
        return handler.next(options);
      },
      onError: (DioException e, handler) {
        return handler.next(e);
      },
    ),
  );

  return dio;
});
