import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:lucide_icons/lucide_icons.dart';
import 'package:go_router/go_router.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';

final profilesListProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final dio = ref.watch(apiClientProvider);
  final response = await dio.get('/api/profiles/list');
  final list = response.data as List<dynamic>;
  return list.map((e) => Map<String, dynamic>.from(e as Map)).toList();
});

IconData getProfileIcon(String? iconName) {
  switch (iconName?.toLowerCase()) {
    case 'smile': return LucideIcons.smile;
    case 'tv': return LucideIcons.tv;
    case 'heart': return LucideIcons.heart;
    case 'star': return LucideIcons.star;
    case 'music': return LucideIcons.music;
    case 'film': return LucideIcons.film;
    case 'sparkles': return LucideIcons.sparkles;
    case 'gamepad-2': return LucideIcons.gamepad2;
    case 'crown': return LucideIcons.crown;
    case 'zap': return LucideIcons.zap;
    case 'camera': return LucideIcons.camera;
    case 'user':
    default: return LucideIcons.user;
  }
}

Color parseProfileColor(String? hexString) {
  if (hexString == null || hexString.isEmpty) return AppTheme.primaryRed;
  try {
    final buffer = StringBuffer();
    if (hexString.length == 6 || hexString.length == 7) buffer.write('ff');
    buffer.write(hexString.replaceFirst('#', ''));
    return Color(int.parse(buffer.toString(), radix: 16));
  } catch (_) {
    return AppTheme.primaryRed;
  }
}

class ProfilePickerScreen extends ConsumerStatefulWidget {
  const ProfilePickerScreen({super.key});

  @override
  ConsumerState<ProfilePickerScreen> createState() => _ProfilePickerScreenState();
}

class _ProfilePickerScreenState extends ConsumerState<ProfilePickerScreen> {
  bool _isManageMode = false;

  void _onProfileTap(Map<String, dynamic> profile) {
    if (_isManageMode) {
      _showEditProfileModal(profile);
      return;
    }

    final hasPin = profile['has_pin'] as bool? ?? false;
    final profileId = profile['id'] as int;

    if (hasPin) {
      _showPinKeypadSheet(profile);
    } else {
      _selectProfileDirect(profileId);
    }
  }

  Future<void> _selectProfileDirect(int profileId) async {
    final dio = ref.read(apiClientProvider);
    final prefs = ref.read(sharedPreferencesProvider);

    try {
      final resp = await dio.post('/api/profile/select', data: {
        'profile_id': profileId,
      });

      if (resp.data['requires_pin'] == true) {
        // Needs PIN verification
        final profiles = await ref.read(profilesListProvider.future);
        final p = profiles.firstWhere((p) => p['id'] == profileId);
        if (mounted) _showPinKeypadSheet(p);
        return;
      }

      await prefs.setInt('active_profile_id', profileId);
      ref.read(activeProfileIdProvider.notifier).state = profileId;
      ref.invalidate(activeProfileInfoProvider);

      if (mounted) {
        context.go('/');
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Failed to switch profile: $e')),
        );
      }
    }
  }

  void _showPinKeypadSheet(Map<String, dynamic> profile) {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      builder: (ctx) => _PinKeypadModal(
        profile: profile,
        onUnlocked: (unlockToken) async {
          final prefs = ref.read(sharedPreferencesProvider);
          final profileId = profile['id'] as int;
          await prefs.setInt('active_profile_id', profileId);
          if (unlockToken != null) {
            await prefs.setString('profile_unlocked_$profileId', unlockToken);
          }
          ref.read(activeProfileIdProvider.notifier).state = profileId;
          ref.invalidate(activeProfileInfoProvider);

          if (ctx.mounted) {
            Navigator.of(ctx).pop();
          }
          if (mounted) {
            context.go('/');
          }
        },
      ),
    );
  }

  void _showEditProfileModal(Map<String, dynamic> profile) {
    final nameCtrl = TextEditingController(text: profile['name']);
    int selectedRating = profile['rating_level'] as int? ?? 0;

    showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          backgroundColor: AppTheme.bgTertiary,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          title: Row(
            children: [
              CircleAvatar(
                backgroundColor: parseProfileColor(profile['avatar_color']),
                radius: 18,
                child: Icon(getProfileIcon(profile['avatar_icon']), color: Colors.white, size: 20),
              ),
              const SizedBox(width: 12),
              Text('Edit ${profile['name']}', style: const TextStyle(fontSize: 18)),
            ],
          ),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              TextField(
                controller: nameCtrl,
                decoration: const InputDecoration(
                  labelText: 'Profile Name',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 16),
              const Text('Maturity Rating:', style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
              const SizedBox(height: 8),
              DropdownButtonFormField<int>(
                value: selectedRating,
                dropdownColor: AppTheme.bgTertiary,
                decoration: const InputDecoration(border: OutlineInputBorder()),
                items: const [
                  DropdownMenuItem(value: 0, child: Text('Adult (All content)')),
                  DropdownMenuItem(value: 1, child: Text('Teen (PG-13 / TV-14)')),
                  DropdownMenuItem(value: 2, child: Text('Child (G, PG, TV-Y)')),
                ],
                onChanged: (val) {
                  if (val != null) setDialogState(() => selectedRating = val);
                },
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('Cancel', style: TextStyle(color: Colors.white70)),
            ),
            ElevatedButton(
              onPressed: () async {
                final dio = ref.read(apiClientProvider);
                try {
                  await dio.post('/api/profile/${profile['id']}/update', data: {
                    'name': nameCtrl.text.trim(),
                    'rating_level': selectedRating,
                    'is_kids': selectedRating == 2,
                  });
                  ref.invalidate(profilesListProvider);
                  if (ctx.mounted) Navigator.of(ctx).pop();
                } catch (e) {
                  if (ctx.mounted) {
                    ScaffoldMessenger.of(ctx).showSnackBar(
                      SnackBar(content: Text('Failed to update: $e')),
                    );
                  }
                }
              },
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
  }

  void _showAddProfileModal() {
    final nameCtrl = TextEditingController();
    int selectedRating = 0;
    String selectedColor = '#e50914';

    final colors = ['#e50914', '#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899'];

    showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          backgroundColor: AppTheme.bgTertiary,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          title: const Text('Add Profile', style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                TextField(
                  controller: nameCtrl,
                  autofocus: true,
                  decoration: const InputDecoration(
                    labelText: 'Profile Name',
                    border: OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 16),
                const Text('Avatar Color:', style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
                const SizedBox(height: 8),
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceAround,
                  children: colors.map((c) {
                    final isSel = selectedColor == c;
                    return GestureDetector(
                      onTap: () => setDialogState(() => selectedColor = c),
                      child: Container(
                        width: 32,
                        height: 32,
                        decoration: BoxDecoration(
                          color: parseProfileColor(c),
                          shape: BoxShape.circle,
                          border: isSel ? Border.all(color: Colors.white, width: 3) : null,
                        ),
                      ),
                    );
                  }).toList(),
                ),
                const SizedBox(height: 16),
                const Text('Maturity Rating:', style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
                const SizedBox(height: 8),
                DropdownButtonFormField<int>(
                  value: selectedRating,
                  dropdownColor: AppTheme.bgTertiary,
                  decoration: const InputDecoration(border: OutlineInputBorder()),
                  items: const [
                    DropdownMenuItem(value: 0, child: Text('Adult (All content)')),
                    DropdownMenuItem(value: 1, child: Text('Teen (PG-13 / TV-14)')),
                    DropdownMenuItem(value: 2, child: Text('Child (G, PG, TV-Y)')),
                  ],
                  onChanged: (val) {
                    if (val != null) setDialogState(() => selectedRating = val);
                  },
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('Cancel', style: TextStyle(color: Colors.white70)),
            ),
            ElevatedButton(
              onPressed: () async {
                final name = nameCtrl.text.trim();
                if (name.isEmpty) return;

                final dio = ref.read(apiClientProvider);
                try {
                  await dio.post('/api/profile/create', data: {
                    'name': name,
                    'avatar_color': selectedColor,
                    'avatar_icon': 'user',
                    'rating_level': selectedRating,
                    'is_kids': selectedRating == 2,
                  });
                  ref.invalidate(profilesListProvider);
                  if (ctx.mounted) Navigator.of(ctx).pop();
                } catch (e) {
                  if (ctx.mounted) {
                    ScaffoldMessenger.of(ctx).showSnackBar(
                      SnackBar(content: Text('Failed to create profile: $e')),
                    );
                  }
                }
              },
              child: const Text('Create'),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final profilesAsync = ref.watch(profilesListProvider);
    final activeId = ref.watch(activeProfileIdProvider);

    return Scaffold(
      backgroundColor: AppTheme.bgPrimary,
      appBar: AppBar(
        automaticallyImplyLeading: false,
        leading: activeId != null
            ? IconButton(
                icon: const Icon(LucideIcons.arrowLeft),
                onPressed: () => context.go('/'),
              )
            : null,
        title: const Text('Heirloom', style: TextStyle(color: AppTheme.primaryRed, fontWeight: FontWeight.bold, fontSize: 24)),
        centerTitle: true,
      ),
      body: profilesAsync.when(
        loading: () => const Center(child: CircularProgressIndicator(color: AppTheme.primaryRed)),
        error: (err, stack) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24.0),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(LucideIcons.alertCircle, color: AppTheme.primaryRed, size: 48),
                const SizedBox(height: 16),
                Text('Error loading profiles:\n$err', textAlign: TextAlign.center, style: const TextStyle(color: Colors.red)),
                const SizedBox(height: 16),
                ElevatedButton.icon(
                  onPressed: () => ref.invalidate(profilesListProvider),
                  icon: const Icon(LucideIcons.refreshCw),
                  label: const Text('Retry'),
                ),
              ],
            ),
          ),
        ),
        data: (profiles) {
          return Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 500),
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Text(
                      _isManageMode ? 'Manage Profiles' : "Who's Watching?",
                      style: const TextStyle(fontSize: 28, fontWeight: FontWeight.bold, color: Colors.white),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      _isManageMode ? 'Tap a profile to edit its settings.' : 'Choose a profile to start watching.',
                      style: const TextStyle(fontSize: 14, color: AppTheme.textTertiary),
                    ),
                    const SizedBox(height: 36),

                    // Grid of Profiles
                    GridView.builder(
                      shrinkWrap: true,
                      physics: const NeverScrollableScrollPhysics(),
                      gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                        crossAxisCount: 2,
                        childAspectRatio: 0.82,
                        crossAxisSpacing: 24,
                        mainAxisSpacing: 24,
                      ),
                      itemCount: profiles.length + (profiles.length < 6 ? 1 : 0),
                      itemBuilder: (context, index) {
                        if (index == profiles.length) {
                          // Add Profile Card
                          return GestureDetector(
                            onTap: _showAddProfileModal,
                            child: Column(
                              children: [
                                AspectRatio(
                                  aspectRatio: 1.0,
                                  child: Container(
                                    decoration: BoxDecoration(
                                      color: AppTheme.bgTertiary,
                                      borderRadius: BorderRadius.circular(16),
                                      border: Border.all(color: Colors.white24, width: 1.5),
                                    ),
                                    child: const Center(
                                      child: Icon(LucideIcons.plus, size: 48, color: AppTheme.textTertiary),
                                    ),
                                  ),
                                ),
                                const SizedBox(height: 10),
                                const Text('Add Profile', style: TextStyle(color: AppTheme.textTertiary, fontWeight: FontWeight.w600, fontSize: 15)),
                              ],
                            ),
                          );
                        }

                        final p = profiles[index];
                        final isPrimary = p['is_primary'] as bool? ?? false;
                        final hasPin = p['has_pin'] as bool? ?? false;
                        final ratingLevel = p['rating_level'] as int? ?? 0;
                        final isKids = p['is_kids'] as bool? ?? false;
                        final isCurrentActive = activeId == p['id'];
                        final color = parseProfileColor(p['avatar_color']);
                        final icon = getProfileIcon(p['avatar_icon']);

                        return GestureDetector(
                          onTap: () => _onProfileTap(p),
                          child: Column(
                            children: [
                              AspectRatio(
                                aspectRatio: 1.0,
                                child: Container(
                                  decoration: BoxDecoration(
                                    color: color,
                                    borderRadius: BorderRadius.circular(16),
                                    border: isCurrentActive
                                        ? Border.all(color: Colors.white, width: 3.5)
                                        : Border.all(color: Colors.transparent, width: 3.5),
                                    boxShadow: [
                                      BoxShadow(
                                        color: color.withOpacity(0.35),
                                        blurRadius: 12,
                                        offset: const Offset(0, 4),
                                      ),
                                    ],
                                  ),
                                  child: Stack(
                                    children: [
                                      Center(
                                        child: Icon(icon, size: 54, color: Colors.white),
                                      ),
                                      if (hasPin)
                                        Positioned(
                                          bottom: 8,
                                          right: 8,
                                          child: Container(
                                            padding: const EdgeInsets.all(4),
                                            decoration: BoxDecoration(
                                              color: Colors.black.withOpacity(0.65),
                                              shape: BoxShape.circle,
                                            ),
                                            child: const Icon(LucideIcons.lock, size: 14, color: Colors.white),
                                          ),
                                        ),
                                      if (_isManageMode)
                                        Positioned(
                                          top: 8,
                                          right: 8,
                                          child: Container(
                                            padding: const EdgeInsets.all(4),
                                            decoration: BoxDecoration(
                                              color: Colors.black.withOpacity(0.75),
                                              shape: BoxShape.circle,
                                            ),
                                            child: const Icon(LucideIcons.pencil, size: 14, color: Colors.white),
                                          ),
                                        ),
                                    ],
                                  ),
                                ),
                              ),
                              const SizedBox(height: 10),
                              Text(
                                p['name'] ?? '',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16, color: Colors.white),
                              ),
                              const SizedBox(height: 2),
                              if (isPrimary)
                                const Text('Owner', style: TextStyle(color: Color(0xFFF5C518), fontSize: 12, fontWeight: FontWeight.bold))
                              else if (ratingLevel == 2 || isKids)
                                const Text('Child', style: TextStyle(color: Color(0xFF4ADE80), fontSize: 12, fontWeight: FontWeight.w600))
                              else if (ratingLevel == 1)
                                const Text('Teen', style: TextStyle(color: Color(0xFF60A5FA), fontSize: 12, fontWeight: FontWeight.w600))
                              else
                                const SizedBox(height: 16),
                            ],
                          ),
                        );
                      },
                    ),

                    const SizedBox(height: 36),

                    // Actions Row
                    Row(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        OutlinedButton.icon(
                          onPressed: () => setState(() => _isManageMode = !_isManageMode),
                          style: OutlinedButton.styleFrom(
                            side: BorderSide(color: _isManageMode ? AppTheme.primaryRed : Colors.white24),
                            foregroundColor: _isManageMode ? AppTheme.primaryRed : Colors.white,
                            padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
                          ),
                          icon: Icon(_isManageMode ? LucideIcons.check : LucideIcons.pencil, size: 16),
                          label: Text(_isManageMode ? 'Done' : 'Manage Profiles'),
                        ),
                        if (activeId != null && !_isManageMode) ...[
                          const SizedBox(width: 16),
                          ElevatedButton.icon(
                            onPressed: () => context.go('/'),
                            style: ElevatedButton.styleFrom(
                              backgroundColor: AppTheme.bgTertiary,
                              foregroundColor: Colors.white,
                              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
                            ),
                            icon: const Icon(LucideIcons.home, size: 16),
                            label: const Text('Back to Home'),
                          ),
                        ],
                      ],
                    ),
                  ],
                ),
              ),
            ),
          );
        },
      ),
    );
  }
}

class _PinKeypadModal extends ConsumerStatefulWidget {
  final Map<String, dynamic> profile;
  final ValueChanged<String?> onUnlocked;

  const _PinKeypadModal({
    required this.profile,
    required this.onUnlocked,
  });

  @override
  ConsumerState<_PinKeypadModal> createState() => _PinKeypadModalState();
}

class _PinKeypadModalState extends ConsumerState<_PinKeypadModal> {
  String _pin = '';
  String? _errorMessage;
  bool _isVerifying = false;

  void _appendDigit(String digit) {
    if (_pin.length < 15) {
      setState(() {
        _pin += digit;
        _errorMessage = null;
      });
      if (_pin.length == 4) {
        _submitPin();
      }
    }
  }

  void _backspace() {
    if (_pin.isNotEmpty) {
      setState(() {
        _pin = _pin.substring(0, _pin.length - 1);
        _errorMessage = null;
      });
    }
  }

  void _clear() {
    setState(() {
      _pin = '';
      _errorMessage = null;
    });
  }

  Future<void> _submitPin() async {
    if (_pin.length < 4 || _isVerifying) return;

    setState(() => _isVerifying = true);
    final dio = ref.read(apiClientProvider);

    try {
      final resp = await dio.post('/api/profile/${widget.profile['id']}/verify-pin', data: {
        'pin': _pin,
      });

      if (resp.data['success'] == true) {
        final unlockToken = resp.data['unlock_token'] as String?;
        widget.onUnlocked(unlockToken);
      } else {
        setState(() {
          _errorMessage = resp.data['error'] ?? 'Incorrect PIN. Please try again.';
          _pin = '';
          _isVerifying = false;
        });
      }
    } catch (e) {
      setState(() {
        _errorMessage = 'Incorrect PIN. Please try again.';
        _pin = '';
        _isVerifying = false;
      });
    }
  }

  void _showForgotPinDialog() {
    final passwordCtrl = TextEditingController();
    bool obscure = true;
    String? forgotError;
    bool isResetting = false;

    showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (context, setForgotState) => AlertDialog(
          backgroundColor: AppTheme.bgTertiary,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          title: const Text('Reset Profile PIN', style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('Enter your account password to reset the PIN on this profile.', style: TextStyle(color: AppTheme.textTertiary, fontSize: 13)),
              const SizedBox(height: 16),
              TextField(
                controller: passwordCtrl,
                obscureText: obscure,
                decoration: InputDecoration(
                  labelText: 'Account Password',
                  border: const OutlineInputBorder(),
                  suffixIcon: IconButton(
                    icon: Icon(obscure ? LucideIcons.eye : LucideIcons.eyeOff, size: 18),
                    onPressed: () => setForgotState(() => obscure = !obscure),
                  ),
                ),
              ),
              if (forgotError != null) ...[
                const SizedBox(height: 8),
                Text(forgotError!, style: const TextStyle(color: Colors.red, fontSize: 12)),
              ],
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('Cancel', style: TextStyle(color: Colors.white70)),
            ),
            ElevatedButton(
              onPressed: isResetting
                  ? null
                  : () async {
                      final pwd = passwordCtrl.text.trim();
                      if (pwd.isEmpty) return;

                      setForgotState(() => isResetting = true);
                      final dio = ref.read(apiClientProvider);

                      try {
                        final resp = await dio.post('/api/profile/${widget.profile['id']}/reset-pin-with-password', data: {
                          'password': pwd,
                        });
                        if (resp.data['success'] == true) {
                          final unlockToken = resp.data['unlock_token'] as String?;
                          if (ctx.mounted) Navigator.of(ctx).pop();
                          widget.onUnlocked(unlockToken);
                        } else {
                          setForgotState(() {
                            forgotError = resp.data['error'] ?? 'Incorrect account password.';
                            isResetting = false;
                          });
                        }
                      } catch (e) {
                        setForgotState(() {
                          forgotError = 'Incorrect account password.';
                          isResetting = false;
                        });
                      }
                    },
              child: Text(isResetting ? 'Verifying...' : 'Reset & Unlock'),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final color = parseProfileColor(widget.profile['avatar_color']);
    final icon = getProfileIcon(widget.profile['avatar_icon']);

    return Container(
      decoration: const BoxDecoration(
        color: AppTheme.bgSecondary,
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
      child: SafeArea(
        top: false,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            // Handle bar
            Container(
              width: 40,
              height: 4,
              decoration: BoxDecoration(
                color: Colors.white24,
                borderRadius: BorderRadius.circular(2),
              ),
            ),
            const SizedBox(height: 20),

            // Profile Avatar & Title
            CircleAvatar(
              backgroundColor: color,
              radius: 32,
              child: Icon(icon, size: 36, color: Colors.white),
            ),
            const SizedBox(height: 12),
            Text(
              "${widget.profile['name']}'s PIN",
              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.bold, color: Colors.white),
            ),
            const SizedBox(height: 4),
            const Text(
              'Enter your PIN to access this profile.',
              style: TextStyle(fontSize: 13, color: AppTheme.textTertiary),
            ),
            const SizedBox(height: 20),

            // PIN Dots Indicator
            Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: List.generate(4, (index) {
                final filled = index < _pin.length;
                return Container(
                  margin: const EdgeInsets.symmetric(horizontal: 8),
                  width: 16,
                  height: 16,
                  decoration: BoxDecoration(
                    color: filled ? AppTheme.primaryRed : Colors.transparent,
                    shape: BoxShape.circle,
                    border: Border.all(
                      color: filled ? AppTheme.primaryRed : Colors.white38,
                      width: 2,
                    ),
                  ),
                );
              }),
            ),

            if (_errorMessage != null) ...[
              const SizedBox(height: 12),
              Text(
                _errorMessage!,
                style: const TextStyle(color: Colors.red, fontSize: 13, fontWeight: FontWeight.w600),
              ),
            ],

            const SizedBox(height: 24),

            // Numeric Keypad Grid
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 300),
              child: Column(
                children: [
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                    children: ['1', '2', '3'].map((d) => _buildKey(d)).toList(),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                    children: ['4', '5', '6'].map((d) => _buildKey(d)).toList(),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                    children: ['7', '8', '9'].map((d) => _buildKey(d)).toList(),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                    children: [
                      _buildActionKey('Clear', _clear),
                      _buildKey('0'),
                      _buildActionKeyIcon(LucideIcons.delete, _backspace),
                    ],
                  ),
                ],
              ),
            ),

            const SizedBox(height: 16),

            // Forgot PIN link
            TextButton(
              onPressed: _showForgotPinDialog,
              child: const Text('Forgot PIN?', style: TextStyle(color: AppTheme.textTertiary, decoration: TextDecoration.underline)),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildKey(String digit) {
    return SizedBox(
      width: 72,
      height: 52,
      child: Material(
        color: AppTheme.bgTertiary,
        borderRadius: BorderRadius.circular(12),
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: () => _appendDigit(digit),
          child: Center(
            child: Text(
              digit,
              style: const TextStyle(fontSize: 22, fontWeight: FontWeight.bold, color: Colors.white),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildActionKey(String label, VoidCallback onTap) {
    return SizedBox(
      width: 72,
      height: 52,
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(12),
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: onTap,
          child: Center(
            child: Text(
              label,
              style: const TextStyle(fontSize: 14, color: AppTheme.textTertiary),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildActionKeyIcon(IconData icon, VoidCallback onTap) {
    return SizedBox(
      width: 72,
      height: 52,
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(12),
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: onTap,
          child: Center(
            child: Icon(icon, size: 20, color: AppTheme.textTertiary),
          ),
        ),
      ),
    );
  }
}
