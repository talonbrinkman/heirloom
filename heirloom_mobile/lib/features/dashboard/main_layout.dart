import 'package:flutter/material.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/theme.dart';
import 'dashboard_home.dart';
import '../library/library_screen.dart';

class MainLayout extends StatefulWidget {
  const MainLayout({super.key});

  @override
  State<MainLayout> createState() => _MainLayoutState();
}

class _MainLayoutState extends State<MainLayout> {
  int _currentIndex = 0;

  final List<Widget> _screens = [
    const DashboardHome(),
    const LibraryScreen(mediaType: 'movie', title: 'Movies'),
    const LibraryScreen(mediaType: 'tv', title: 'TV Shows'),
    const LibraryScreen(mediaType: 'photo', title: 'Photos'),
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: _screens[_currentIndex],
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _currentIndex,
        onTap: (index) => setState(() => _currentIndex = index),
        type: BottomNavigationBarType.fixed,
        backgroundColor: AppTheme.bgSecondary,
        selectedItemColor: AppTheme.primaryRed,
        unselectedItemColor: AppTheme.textTertiary,
        items: const [
          BottomNavigationBarItem(
            icon: Icon(LucideIcons.home),
            label: 'Home',
          ),
          BottomNavigationBarItem(
            icon: Icon(LucideIcons.film),
            label: 'Movies',
          ),
          BottomNavigationBarItem(
            icon: Icon(LucideIcons.tv),
            label: 'TV Shows',
          ),
          BottomNavigationBarItem(
            icon: Icon(LucideIcons.image),
            label: 'Photos',
          ),
        ],
      ),
    );
  }
}
