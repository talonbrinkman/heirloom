import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/theme.dart';
import 'core/api_client.dart';
import 'features/setup/setup_screen.dart';
import 'features/auth/login_screen.dart';
import 'features/dashboard/main_layout.dart';
import 'features/media/media_details_screen.dart';
import 'features/media/video_player_screen.dart';
import 'features/settings/settings_screen.dart';
import 'features/search/search_screen.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  
  final prefs = await SharedPreferences.getInstance();

  runApp(
    ProviderScope(
      overrides: [
        sharedPreferencesProvider.overrideWithValue(prefs),
      ],
      child: const HeirloomApp(),
    ),
  );
}

class HeirloomApp extends ConsumerWidget {
  const HeirloomApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final serverUrl = ref.watch(serverUrlProvider);
    final authToken = ref.watch(authTokenProvider);

    final router = GoRouter(
      initialLocation: _getInitialLocation(serverUrl, authToken),
      routes: [
        GoRoute(
          path: '/setup',
          builder: (context, state) => const SetupScreen(),
        ),
        GoRoute(
          path: '/login',
          builder: (context, state) => const LoginScreen(),
        ),
        GoRoute(
          path: '/',
          builder: (context, state) => const MainLayout(),
        ),
        GoRoute(
          path: '/details/:mediaType/:id',
          builder: (context, state) {
            final mediaType = state.pathParameters['mediaType']!;
            final id = state.pathParameters['id']!;
            return MediaDetailsScreen(mediaType: mediaType, id: id);
          },
        ),
        GoRoute(
          path: '/play/:mediaType/:id',
          builder: (context, state) {
            final mediaType = state.pathParameters['mediaType']!;
            final id = state.pathParameters['id']!;
            return VideoPlayerScreen(mediaType: mediaType, id: id);
          },
        ),
        GoRoute(
          path: '/settings',
          builder: (context, state) => const SettingsScreen(),
        ),
        GoRoute(
          path: '/search',
          builder: (context, state) => const SearchScreen(),
        ),
      ],
      redirect: (context, state) {
        // Enforce flow: Setup -> Login -> Dashboard
        final isSetup = serverUrl != null && serverUrl.isNotEmpty;
        final isLogged = authToken != null && authToken.isNotEmpty;
        
        final goingToSetup = state.matchedLocation == '/setup';
        final goingToLogin = state.matchedLocation == '/login';

        if (!isSetup && !goingToSetup) return '/setup';
        if (isSetup && !isLogged && !goingToLogin && !goingToSetup) return '/login';
        if (isSetup && isLogged && (goingToSetup || goingToLogin)) return '/';
        
        return null;
      },
    );

    return MaterialApp.router(
      title: 'Heirloom',
      theme: AppTheme.darkTheme,
      routerConfig: router,
      debugShowCheckedModeBanner: false,
    );
  }

  String _getInitialLocation(String? serverUrl, String? authToken) {
    if (serverUrl == null || serverUrl.isEmpty) {
      return '/setup';
    }
    if (authToken == null || authToken.isEmpty) {
      return '/login';
    }
    return '/';
  }
}
