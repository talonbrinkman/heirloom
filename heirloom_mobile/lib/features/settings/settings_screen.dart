import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:go_router/go_router.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../auth/profile_picker_screen.dart';

final settingsDataProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/settings');
  return Map<String, dynamic>.from(response.data as Map);
});

class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});

  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  int? _rewindSeconds;
  int? _autoPauseEpisodes;
  String? _defaultAudioLang;
  String? _defaultSubtitleLang;
  String? _saveStatusMessage;

  @override
  Widget build(BuildContext context) {
    final settingsAsync = ref.watch(settingsDataProvider);
    final serverUrl = ref.watch(serverUrlProvider) ?? '';

    return Scaffold(
      backgroundColor: AppTheme.bgPrimary,
      appBar: AppBar(
        title: const Text('Settings', style: TextStyle(fontWeight: FontWeight.bold)),
        leading: IconButton(
          icon: const Icon(LucideIcons.arrowLeft),
          onPressed: () => context.pop(),
        ),
        actions: [
          if (_saveStatusMessage != null)
            Center(
              child: Padding(
                padding: const EdgeInsets.only(right: 16.0),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(LucideIcons.check, size: 16, color: Color(0xFF4ADE80)),
                    const SizedBox(width: 4),
                    Text(
                      _saveStatusMessage!,
                      style: const TextStyle(color: Color(0xFF4ADE80), fontSize: 13, fontWeight: FontWeight.bold),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
      body: settingsAsync.when(
        loading: () => const Center(child: CircularProgressIndicator(color: AppTheme.primaryRed)),
        error: (err, stack) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24.0),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(LucideIcons.alertCircle, color: AppTheme.primaryRed, size: 48),
                const SizedBox(height: 16),
                Text('Error loading settings:\n$err', textAlign: TextAlign.center, style: const TextStyle(color: Colors.red)),
                const SizedBox(height: 16),
                ElevatedButton.icon(
                  onPressed: () => ref.invalidate(settingsDataProvider),
                  icon: const Icon(LucideIcons.refreshCw),
                  label: const Text('Retry'),
                ),
              ],
            ),
          ),
        ),
        data: (data) {
          final user = Map<String, dynamic>.from(data['user'] as Map? ?? {});
          final activeProfile = Map<String, dynamic>.from(data['active_profile'] as Map? ?? {});
          final prefs = Map<String, dynamic>.from(data['preferences'] as Map? ?? {});
          final availableLangs = (data['available_languages'] as List? ?? []).map((e) => Map<String, dynamic>.from(e as Map)).toList();
          final server = Map<String, dynamic>.from(data['server'] as Map? ?? {});

          _rewindSeconds ??= prefs['rewind_seconds'] as int? ?? 0;
          _autoPauseEpisodes ??= prefs['auto_pause_episodes'] as int? ?? 3;
          _defaultAudioLang ??= prefs['default_audio_lang'] as String? ?? 'eng';
          _defaultSubtitleLang ??= prefs['default_subtitle_lang'] as String? ?? 'off';

          return RefreshIndicator(
            onRefresh: () => ref.refresh(settingsDataProvider.future),
            child: ListView(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 20),
              children: [
                // 1. ACTIVE PROFILE CARD
                _buildActiveProfileCard(activeProfile),
                const SizedBox(height: 16),

                // 2. MY ACCOUNT CARD
                _buildAccountCard(user),
                const SizedBox(height: 16),

                // 3. PLAYBACK & REWIND CARD
                _buildRewindCard(activeProfile),
                const SizedBox(height: 16),

                // 4. AUDIO & SUBTITLE PREFERENCES
                _buildLanguageCard(availableLangs, activeProfile),
                const SizedBox(height: 16),

                // 5. AUTO-PAUSE CARD
                _buildAutoPauseCard(activeProfile),
                const SizedBox(height: 16),

                // 6. SERVER & CONNECTIVITY
                _buildServerCard(serverUrl, server),
                const SizedBox(height: 24),

                // 7. LOG OUT BUTTON
                _buildLogOutButton(),
                const SizedBox(height: 32),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _buildCard({required Widget child}) {
    return Container(
      decoration: BoxDecoration(
        color: AppTheme.bgTertiary,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: Colors.white10),
      ),
      padding: const EdgeInsets.all(16),
      child: child,
    );
  }

  // --- 1. ACTIVE PROFILE CARD ---
  Widget _buildActiveProfileCard(Map<String, dynamic> profile) {
    final name = profile['name'] as String? ?? 'Profile';
    final isPrimary = profile['is_primary'] as bool? ?? false;
    final ratingLevel = profile['rating_level'] as int? ?? 0;
    final isKids = profile['is_kids'] as bool? ?? false;
    final hasPin = profile['has_pin'] as bool? ?? false;
    final color = parseProfileColor(profile['avatar_color']);
    final icon = getProfileIcon(profile['avatar_icon']);

    return _buildCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              CircleAvatar(
                backgroundColor: color,
                radius: 26,
                child: Icon(icon, size: 28, color: Colors.white),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      name,
                      style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: Colors.white),
                    ),
                    const SizedBox(height: 4),
                    Row(
                      children: [
                        if (isPrimary)
                          _buildBadge('Owner', const Color(0xFFF5C518))
                        else if (ratingLevel == 2 || isKids)
                          _buildBadge('Child', const Color(0xFF4ADE80))
                        else if (ratingLevel == 1)
                          _buildBadge('Teen', const Color(0xFF60A5FA))
                        else
                          _buildBadge('Adult', Colors.white70),
                        const SizedBox(width: 8),
                        if (hasPin)
                          const Row(
                            children: [
                              Icon(LucideIcons.lock, size: 12, color: AppTheme.textTertiary),
                              SizedBox(width: 3),
                              Text('PIN Locked', style: TextStyle(color: AppTheme.textTertiary, fontSize: 11)),
                            ],
                          ),
                      ],
                    ),
                  ],
                ),
              ),
              OutlinedButton.icon(
                onPressed: () => context.push('/profiles'),
                style: OutlinedButton.styleFrom(
                  side: const BorderSide(color: Colors.white24),
                  foregroundColor: Colors.white,
                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                ),
                icon: const Icon(LucideIcons.users, size: 14),
                label: const Text('Switch', style: TextStyle(fontSize: 12)),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildBadge(String text, Color color) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: color.withOpacity(0.15),
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: color.withOpacity(0.4)),
      ),
      child: Text(
        text,
        style: TextStyle(color: color, fontSize: 10, fontWeight: FontWeight.bold),
      ),
    );
  }

  // --- 2. MY ACCOUNT CARD ---
  Widget _buildAccountCard(Map<String, dynamic> user) {
    final username = user['username'] as String? ?? 'User';
    final isAdmin = user.isNotEmpty && (user['is_admin'] as bool? ?? false);
    final hasMovies = user['movies_access'] as bool? ?? false;
    final hasTv = user['tv_access'] as bool? ?? false;
    final hasPhotos = user['photos_access'] as bool? ?? false;
    final hasMusic = user['music_access'] as bool? ?? false;

    return _buildCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text('My Account: $username', style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
              _buildBadge(isAdmin ? 'Admin' : 'Member', isAdmin ? AppTheme.primaryRed : Colors.white70),
            ],
          ),
          const SizedBox(height: 12),
          const Text('Library Permissions:', style: TextStyle(color: AppTheme.textTertiary, fontSize: 12)),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 6,
            children: [
              _buildAccessChip('Movies', hasMovies, LucideIcons.film),
              _buildAccessChip('TV Shows', hasTv, LucideIcons.tv),
              _buildAccessChip('Photos', hasPhotos, LucideIcons.image),
              _buildAccessChip('Music', hasMusic, LucideIcons.music),
            ],
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton.icon(
              onPressed: _showChangePasswordDialog,
              style: OutlinedButton.styleFrom(
                side: const BorderSide(color: Colors.white24),
                foregroundColor: Colors.white,
                padding: const EdgeInsets.symmetric(vertical: 10),
              ),
              icon: const Icon(LucideIcons.keyRound, size: 16),
              label: const Text('Change Password'),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildAccessChip(String label, bool enabled, IconData icon) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: enabled ? AppTheme.primaryRed.withOpacity(0.15) : Colors.white.withOpacity(0.04),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: enabled ? AppTheme.primaryRed.withOpacity(0.4) : Colors.white12),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 12, color: enabled ? AppTheme.primaryRed : Colors.white30),
          const SizedBox(width: 4),
          Text(label, style: TextStyle(color: enabled ? Colors.white : Colors.white38, fontSize: 11, fontWeight: FontWeight.w500)),
        ],
      ),
    );
  }

  // --- 3. REWIND ON RESUME CARD ---
  Widget _buildRewindCard(Map<String, dynamic> activeProfile) {
    final profileName = activeProfile['name'] as String?;
    final presets = [0, 5, 10, 15, 30];

    return _buildCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Expanded(
                child: Text(
                  'Rewind on Resume${profileName != null ? ' ($profileName)' : ''}',
                  style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  color: AppTheme.primaryRed.withOpacity(0.2),
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Text(
                  '${_rewindSeconds}s',
                  style: const TextStyle(color: AppTheme.primaryRed, fontWeight: FontWeight.bold, fontSize: 13),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          const Text(
            'Seconds playback will automatically rewind when resuming a video with saved progress. Set to 0 to disable.',
            style: TextStyle(color: AppTheme.textTertiary, fontSize: 12),
          ),
          const SizedBox(height: 14),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: presets.map((s) {
              final isSelected = _rewindSeconds == s;
              return ChoiceChip(
                label: Text(s == 0 ? 'Off (0s)' : '${s}s'),
                selected: isSelected,
                selectedColor: AppTheme.primaryRed,
                backgroundColor: AppTheme.bgSecondary,
                labelStyle: TextStyle(
                  color: isSelected ? Colors.white : AppTheme.textTertiary,
                  fontWeight: isSelected ? FontWeight.bold : FontWeight.normal,
                ),
                onSelected: (selected) {
                  if (selected) {
                    setState(() => _rewindSeconds = s);
                    _saveSetting({'rewind_seconds': s});
                  }
                },
              );
            }).toList(),
          ),
        ],
      ),
    );
  }

  // --- 4. LANGUAGE PREFERENCES CARD ---
  Widget _buildLanguageCard(List<Map<String, dynamic>> languages, Map<String, dynamic> activeProfile) {
    final profileName = activeProfile['name'] as String?;

    return _buildCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Audio & Subtitle Preferences${profileName != null ? ' ($profileName)' : ''}',
            style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
          ),
          const SizedBox(height: 6),
          const Text(
            'Default audio and subtitle tracks applied when starting a video.',
            style: TextStyle(color: AppTheme.textTertiary, fontSize: 12),
          ),
          const SizedBox(height: 14),

          // Default Audio Language Dropdown
          const Text('Default Audio Language', style: TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
          const SizedBox(height: 6),
          DropdownButtonFormField<String>(
            value: languages.any((l) => l['code'] == _defaultAudioLang) ? _defaultAudioLang : 'eng',
            dropdownColor: AppTheme.bgTertiary,
            decoration: const InputDecoration(
              contentPadding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              border: OutlineInputBorder(),
            ),
            items: languages.map((lang) {
              return DropdownMenuItem<String>(
                value: lang['code'] as String,
                child: Text(lang['name'] as String),
              );
            }).toList(),
            onChanged: (val) {
              if (val != null) {
                setState(() => _defaultAudioLang = val);
                _saveSetting({'default_audio_lang': val});
              }
            },
          ),
          const SizedBox(height: 14),

          // Default Subtitle Language Dropdown
          const Text('Default Subtitle Language', style: TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
          const SizedBox(height: 6),
          DropdownButtonFormField<String>(
            value: (_defaultSubtitleLang == 'off' || languages.any((l) => l['code'] == _defaultSubtitleLang)) ? _defaultSubtitleLang : 'off',
            dropdownColor: AppTheme.bgTertiary,
            decoration: const InputDecoration(
              contentPadding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              border: OutlineInputBorder(),
            ),
            items: [
              const DropdownMenuItem<String>(value: 'off', child: Text('Off')),
              ...languages.map((lang) {
                return DropdownMenuItem<String>(
                  value: lang['code'] as String,
                  child: Text(lang['name'] as String),
                );
              }),
            ],
            onChanged: (val) {
              if (val != null) {
                setState(() => _defaultSubtitleLang = val);
                _saveSetting({'default_subtitle_lang': val});
              }
            },
          ),
        ],
      ),
    );
  }

  // --- 5. AUTO-PAUSE CARD ---
  Widget _buildAutoPauseCard(Map<String, dynamic> activeProfile) {
    final profileName = activeProfile['name'] as String?;

    return _buildCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Playback & Auto-Pause${profileName != null ? ' ($profileName)' : ''}',
            style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
          ),
          const SizedBox(height: 6),
          const Text(
            'Automatically pause continuous playback when inactive to prevent runaway streaming.',
            style: TextStyle(color: AppTheme.textTertiary, fontSize: 12),
          ),
          const SizedBox(height: 14),
          const Text('"Are You Still Watching?" Prompt', style: TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
          const SizedBox(height: 6),
          DropdownButtonFormField<int>(
            value: _autoPauseEpisodes,
            dropdownColor: AppTheme.bgTertiary,
            decoration: const InputDecoration(
              contentPadding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              border: OutlineInputBorder(),
            ),
            items: const [
              DropdownMenuItem(value: 2, child: Text('After 2 episodes')),
              DropdownMenuItem(value: 3, child: Text('After 3 episodes (Recommended)')),
              DropdownMenuItem(value: 4, child: Text('After 4 episodes')),
              DropdownMenuItem(value: 5, child: Text('After 5 episodes')),
              DropdownMenuItem(value: 0, child: Text('Never (Continuous Playback)')),
            ],
            onChanged: (val) {
              if (val != null) {
                setState(() => _autoPauseEpisodes = val);
                _saveSetting({'auto_pause_episodes': val});
              }
            },
          ),
        ],
      ),
    );
  }

  // --- 6. SERVER & CONNECTIVITY ---
  Widget _buildServerCard(String serverUrl, Map<String, dynamic> server) {
    final version = server['version'] as String? ?? '1.7.4';

    return _buildCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              const Text('Server Connection', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  color: const Color(0xFF4ADE80).withOpacity(0.15),
                  borderRadius: BorderRadius.circular(6),
                  border: Border.all(color: const Color(0xFF4ADE80).withOpacity(0.4)),
                ),
                child: const Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(LucideIcons.checkCircle2, size: 12, color: Color(0xFF4ADE80)),
                    SizedBox(width: 4),
                    Text('Online', style: TextStyle(color: Color(0xFF4ADE80), fontSize: 11, fontWeight: FontWeight.bold)),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Text(serverUrl, style: const TextStyle(color: Colors.white, fontSize: 13, fontFamily: 'monospace')),
          const SizedBox(height: 4),
          Text('Heirloom Server v$version', style: const TextStyle(color: AppTheme.textTertiary, fontSize: 12)),
          const SizedBox(height: 14),
          OutlinedButton.icon(
            onPressed: () {
              // Reconnect or switch server
              context.push('/setup');
            },
            style: OutlinedButton.styleFrom(
              side: const BorderSide(color: Colors.white24),
              foregroundColor: Colors.white,
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
            ),
            icon: const Icon(LucideIcons.server, size: 14),
            label: const Text('Change Server URL', style: TextStyle(fontSize: 12)),
          ),
        ],
      ),
    );
  }

  // --- 7. LOG OUT BUTTON ---
  Widget _buildLogOutButton() {
    return SizedBox(
      width: double.infinity,
      child: OutlinedButton.icon(
        onPressed: _showLogOutConfirmation,
        style: OutlinedButton.styleFrom(
          side: const BorderSide(color: AppTheme.primaryRed, width: 1.5),
          foregroundColor: AppTheme.primaryRed,
          padding: const EdgeInsets.symmetric(vertical: 14),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
        ),
        icon: const Icon(LucideIcons.logOut, size: 18),
        label: const Text('Sign Out of Heirloom', style: TextStyle(fontWeight: FontWeight.bold, fontSize: 15)),
      ),
    );
  }

  Future<void> _saveSetting(Map<String, dynamic> data) async {
    final dio = ref.read(apiClientProvider);

    try {
      await dio.post('/api/settings', data: data);
      if (mounted) {
        setState(() {
          _saveStatusMessage = 'Saved';
        });
        Future.delayed(const Duration(seconds: 2), () {
          if (mounted) setState(() => _saveStatusMessage = null);
        });
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Failed to save settings: $e')),
        );
      }
    }
  }

  void _showChangePasswordDialog() {
    final passwordCtrl = TextEditingController();
    bool obscure = true;
    bool isSaving = false;

    showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setPassState) => AlertDialog(
          backgroundColor: AppTheme.bgTertiary,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          title: const Text('Change Account Password', style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('Enter your new password below.', style: TextStyle(color: AppTheme.textTertiary, fontSize: 13)),
              const SizedBox(height: 16),
              TextField(
                controller: passwordCtrl,
                obscureText: obscure,
                decoration: InputDecoration(
                  labelText: 'New Password',
                  border: const OutlineInputBorder(),
                  suffixIcon: IconButton(
                    icon: Icon(obscure ? LucideIcons.eye : LucideIcons.eyeOff, size: 18),
                    onPressed: () => setPassState(() => obscure = !obscure),
                  ),
                ),
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('Cancel', style: TextStyle(color: Colors.white70)),
            ),
            ElevatedButton(
              onPressed: isSaving
                  ? null
                  : () async {
                      final pwd = passwordCtrl.text.trim();
                      if (pwd.length < 6) {
                        ScaffoldMessenger.of(context).showSnackBar(
                          const SnackBar(content: Text('Password must be at least 6 characters.')),
                        );
                        return;
                      }

                      setPassState(() => isSaving = true);
                      final dio = ref.read(apiClientProvider);

                      try {
                        await dio.post('/api/settings', data: {'new_password': pwd});
                        if (ctx.mounted) {
                          Navigator.of(ctx).pop();
                          ScaffoldMessenger.of(ctx).showSnackBar(
                            const SnackBar(content: Text('Password changed successfully!')),
                          );
                        }
                      } catch (e) {
                        if (ctx.mounted) {
                          setPassState(() => isSaving = false);
                          ScaffoldMessenger.of(ctx).showSnackBar(
                            SnackBar(content: Text('Failed to change password: $e')),
                          );
                        }
                      }
                    },
              child: Text(isSaving ? 'Saving...' : 'Update Password'),
            ),
          ],
        ),
      ),
    );
  }

  void _showLogOutConfirmation() {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppTheme.bgTertiary,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        title: const Text('Sign Out', style: TextStyle(fontWeight: FontWeight.bold)),
        content: const Text('Are you sure you want to sign out of Heirloom?'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancel', style: TextStyle(color: Colors.white70)),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(backgroundColor: AppTheme.primaryRed),
            onPressed: () async {
              Navigator.of(ctx).pop();
              final prefs = ref.read(sharedPreferencesProvider);
              await prefs.remove('auth_token');
              await prefs.remove('active_profile_id');
              ref.read(authTokenProvider.notifier).state = null;
              ref.read(activeProfileIdProvider.notifier).state = null;
              if (mounted) {
                context.go('/login');
              }
            },
            child: const Text('Sign Out'),
          ),
        ],
      ),
    );
  }
}
