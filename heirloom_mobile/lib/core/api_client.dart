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

// Dio API Client Provider
final apiClientProvider = Provider<Dio>((ref) {
  final serverUrl = ref.watch(serverUrlProvider);
  final authToken = ref.watch(authTokenProvider);

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

  // Add an interceptor to automatically inject the Bearer token
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        if (authToken != null && authToken.isNotEmpty) {
          options.headers['Authorization'] = 'Bearer $authToken';
        }
        return handler.next(options);
      },
      onError: (DioException e, handler) {
        // Handle global errors here if needed (e.g., 401 Unauthorized -> logout)
        return handler.next(e);
      },
    ),
  );

  return dio;
});
