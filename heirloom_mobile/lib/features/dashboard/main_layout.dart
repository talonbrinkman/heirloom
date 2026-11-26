import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../library/library_screen.dart';
import '../music/mini_player_bar.dart';
import 'dashboard_home.dart';

class _NavTab {
  final String key;
  final String label;
  final IconData icon;
  final Widget screen;

  const _NavTab({
    required this.key,
    required this.label,
    required this.icon,
    required this.screen,
  });
}

class MainLayout extends ConsumerStatefulWidget {
  const MainLayout({super.key});

  @override
  ConsumerState<MainLayout> createState() => _MainLayoutState();
}

class _MainLayoutState extends ConsumerState<MainLayout> {
  int _currentIndex = 0;

  List<_NavTab> _buildTabs(AppSessionState session) {
    final features = session.features;
    final user = session.userInfo;

    bool hasAccess(String featureKey, String accessKey) {
      final featureOn = features[featureKey] != false;
      final userAllowed = user.isEmpty || user[accessKey] != false;
      return featureOn && userAllowed;
    }

    return [
      const _NavTab(
        key: 'home',
        label: 'Home',
        icon: LucideIcons.home,
        screen: DashboardHome(),
      ),
      if (hasAccess('movies', 'movies_access'))
        const _NavTab(
          key: 'movie',
          label: 'Movies',
          icon: LucideIcons.film,
          screen: LibraryScreen(mediaType: 'movie', title: 'Movies'),
        ),
      if (hasAccess('tv', 'tv_access'))
        const _NavTab(
          key: 'tv',
          label: 'TV Shows',
          icon: LucideIcons.tv,
          screen: LibraryScreen(mediaType: 'tv', title: 'TV Shows'),
        ),
      if (hasAccess('photos', 'photos_access'))
        const _NavTab(
          key: 'photo',
          label: 'Photos',
          icon: LucideIcons.image,
          screen: LibraryScreen(mediaType: 'photo', title: 'Photos'),
        ),
      if (hasAccess('music', 'music_access'))
        const _NavTab(
          key: 'music',
          label: 'Music',
          icon: LucideIcons.music,
          screen: LibraryScreen(mediaType: 'music', title: 'Music'),
        ),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(appSessionProvider);
    final tabs = _buildTabs(session);
    final safeIndex = _currentIndex.clamp(0, tabs.length - 1);

    return Scaffold(
      body: Column(
        children: [
          Expanded(
            child: IndexedStack(
              index: safeIndex,
              children: tabs.map((t) => t.screen).toList(),
            ),
          ),
          const MiniPlayerBar(),
        ],
      ),
      bottomNavigationBar: tabs.length <= 1
          ? null
          : BottomNavigationBar(
              currentIndex: safeIndex,
              onTap: (index) => setState(() => _currentIndex = index),
              type: BottomNavigationBarType.fixed,
              backgroundColor: AppColors.bg,
              selectedItemColor: AppColors.accent,
              unselectedItemColor: AppColors.textMuted,
              selectedFontSize: 11,
              unselectedFontSize: 11,
              items: tabs
                  .map(
                    (t) => BottomNavigationBarItem(
                      icon: Icon(t.icon),
                      label: t.label,
                    ),
                  )
                  .toList(),
            ),
    );
  }
}
