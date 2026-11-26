import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/api_client.dart';
import 'core/theme.dart';
import 'features/auth/login_screen.dart';
import 'features/auth/profile_picker_screen.dart';
import 'features/dashboard/main_layout.dart';
import 'features/media/media_details_screen.dart';
import 'features/media/video_player_screen.dart';
import 'features/music/album_screen.dart';
import 'features/music/artist_screen.dart';
import 'features/music/playlist_screen.dart';
import 'features/search/search_screen.dart';
import 'features/settings/settings_screen.dart';
import 'features/setup/setup_screen.dart';
import 'features/social/inbox_screen.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final prefs = await SharedPreferences.getInstance();

  final container = ProviderContainer(
    overrides: [
      sharedPreferencesProvider.overrideWithValue(prefs),
    ],
  );
  await container.read(appSessionProvider.notifier).init();

  runApp(
    UncontrolledProviderScope(
      container: container,
      child: const HeirloomApp(),
    ),
  );
}

class HeirloomApp extends ConsumerWidget {
  const HeirloomApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(appSessionProvider);

    final router = GoRouter(
      initialLocation: _getInitialLocation(session),
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
          path: '/profiles',
          builder: (context, state) => const ProfilePickerScreen(),
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
            final restart = state.uri.queryParameters['restart'] == '1';
            return VideoPlayerScreen(mediaType: mediaType, id: id, restart: restart);
          },
        ),
        GoRoute(
          path: '/album/:id',
          builder: (context, state) => AlbumScreen(albumId: state.pathParameters['id']!),
        ),
        GoRoute(
          path: '/artist/:id',
          builder: (context, state) => ArtistScreen(artistId: state.pathParameters['id']!),
        ),
        GoRoute(
          path: '/playlist/:id',
          builder: (context, state) => PlaylistScreen(playlistId: state.pathParameters['id']!),
        ),
        GoRoute(
          path: '/inbox',
          builder: (context, state) => const InboxScreen(),
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
        final isSetup = session.isSetup;
        final isLogged = session.isAuthenticated;
        final hasProfile = session.activeProfileId != null;

        final goingToSetup = state.matchedLocation == '/setup';
        final goingToLogin = state.matchedLocation == '/login';
        final goingToProfiles = state.matchedLocation == '/profiles';

        if (!isSetup && !goingToSetup) return '/setup';
        if (isSetup && !isLogged && !goingToLogin && !goingToSetup) return '/login';
        if (isSetup && isLogged && !hasProfile && !goingToProfiles && !goingToSetup) {
          return '/profiles';
        }
        if (isSetup && isLogged && goingToLogin) {
          return hasProfile ? '/' : '/profiles';
        }
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

  String _getInitialLocation(AppSessionState session) {
    if (!session.isSetup) return '/setup';
    if (!session.isAuthenticated) return '/login';
    if (session.activeProfileId == null) return '/profiles';
    return '/';
  }
}
