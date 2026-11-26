import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:lucide_icons/lucide_icons.dart';
import '../../core/api_client.dart';
import '../../core/theme.dart';

class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});

  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final _usernameController = TextEditingController();
  final _passwordController = TextEditingController();
  final _forgotUsernameController = TextEditingController();

  bool _isLoading = false;
  bool _obscurePassword = true;
  bool _rememberMe = true; // Matches web login.html "Keep me signed in" default checked
  bool _showForgotPassword = false;
  String? _error;
  String? _notice;

  @override
  void initState() {
    super.initState();
    _prefillSavedCredentials();
  }

  Future<void> _prefillSavedCredentials() async {
    final session = ref.read(appSessionProvider);
    final srv = session.activeServer;
    if (srv == null) return;
    if (srv.lastUsername != null && srv.lastUsername!.isNotEmpty) {
      _usernameController.text = srv.lastUsername!;
    }
    final savedPassword = await ref.read(appSessionProvider.notifier).getSavedPasswordForActiveServer();
    if (!mounted) return;
    setState(() {
      _rememberMe = srv.rememberMe;
      if (savedPassword != null && savedPassword.isNotEmpty && _passwordController.text.isEmpty) {
        _passwordController.text = savedPassword;
      }
    });
  }

  @override
  void dispose() {
    _usernameController.dispose();
    _passwordController.dispose();
    _forgotUsernameController.dispose();
    super.dispose();
  }

  Future<void> _login() async {
    final username = _usernameController.text.trim();
    final password = _passwordController.text;

    if (username.isEmpty || password.isEmpty) {
      setState(() => _error = 'Please enter both username and password');
      return;
    }

    setState(() {
      _isLoading = true;
      _error = null;
      _notice = null;
    });

    try {
      await ref.read(appSessionProvider.notifier).login(
            username,
            password,
            rememberMe: _rememberMe,
          );
      TextInput.finishAutofillContext(shouldSave: _rememberMe);

      final updated = ref.read(appSessionProvider);
      if (!mounted) return;
      context.go(updated.activeProfileId != null ? '/' : '/profiles');
    } catch (e) {
      if (mounted) {
        setState(() => _error = formatApiError(e, isLogin: true));
      }
    } finally {
      if (mounted) {
        setState(() => _isLoading = false);
      }
    }
  }

  Future<void> _requestPasswordReset() async {
    final username = _forgotUsernameController.text.trim();
    if (username.isEmpty) {
      setState(() => _error = 'Enter your username to request a reset.');
      return;
    }
    setState(() {
      _isLoading = true;
      _error = null;
      _notice = null;
    });
    try {
      final dio = ref.read(apiClientProvider);
      await dio.post('/forgot_password', data: {'username': username});
      if (mounted) {
        setState(() {
          _showForgotPassword = false;
          _notice = 'Password reset request sent. An admin will review it shortly.';
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() => _error = 'Could not submit password reset request.');
      }
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final session = ref.watch(appSessionProvider);
    final serverName = session.activeServer?.name ?? 'Heirloom';
    final serverUrl = session.serverUrl ?? '';

    return Scaffold(
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24.0),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 420),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Icon(LucideIcons.clapperboard, size: 56, color: AppColors.accent),
                const SizedBox(height: 16),
                Text(
                  _showForgotPassword ? 'Reset Password' : 'Login',
                  textAlign: TextAlign.center,
                  style: const TextStyle(fontSize: 28, fontWeight: FontWeight.bold, color: AppColors.text),
                ),
                const SizedBox(height: 8),
                Text(
                  '$serverName • $serverUrl',
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: AppColors.textMuted, fontSize: 13),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
                TextButton.icon(
                  onPressed: () => context.go('/setup'),
                  icon: const Icon(LucideIcons.server, size: 15),
                  label: const Text('Switch Server'),
                  style: TextButton.styleFrom(foregroundColor: AppColors.textMuted),
                ),
                const SizedBox(height: 20),
                if (_error != null)
                  Container(
                    padding: const EdgeInsets.all(12),
                    margin: const EdgeInsets.only(bottom: 16),
                    decoration: BoxDecoration(
                      color: AppColors.accentTransparent,
                      borderRadius: BorderRadius.circular(AppSpacing.radius),
                    ),
                    child: Text(_error!, style: const TextStyle(color: AppColors.accent), textAlign: TextAlign.center),
                  ),
                if (_notice != null)
                  Container(
                    padding: const EdgeInsets.all(12),
                    margin: const EdgeInsets.only(bottom: 16),
                    decoration: BoxDecoration(
                      color: AppColors.success.withOpacity(0.15),
                      borderRadius: BorderRadius.circular(AppSpacing.radius),
                    ),
                    child: Text(_notice!, style: const TextStyle(color: AppColors.success), textAlign: TextAlign.center),
                  ),
                if (!_showForgotPassword)
                  AutofillGroup(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        TextField(
                          controller: _usernameController,
                          autofillHints: const [AutofillHints.username],
                          decoration: const InputDecoration(
                            labelText: 'Username',
                            prefixIcon: Icon(LucideIcons.user),
                          ),
                          textInputAction: TextInputAction.next,
                        ),
                        const SizedBox(height: 14),
                        TextField(
                          controller: _passwordController,
                          autofillHints: const [AutofillHints.password],
                          obscureText: _obscurePassword,
                          decoration: InputDecoration(
                            labelText: 'Password',
                            prefixIcon: const Icon(LucideIcons.lock),
                            suffixIcon: IconButton(
                              icon: Icon(
                                _obscurePassword ? LucideIcons.eye : LucideIcons.eyeOff,
                                size: 18,
                                color: AppColors.textMuted,
                              ),
                              onPressed: () => setState(() => _obscurePassword = !_obscurePassword),
                            ),
                          ),
                          onSubmitted: (_) => _login(),
                        ),
                        const SizedBox(height: 10),
                        InkWell(
                          borderRadius: BorderRadius.circular(AppSpacing.radius),
                          onTap: () => setState(() => _rememberMe = !_rememberMe),
                          child: Row(
                            children: [
                              Checkbox(
                                value: _rememberMe,
                                onChanged: (val) => setState(() => _rememberMe = val ?? true),
                              ),
                              const Text(
                                'Keep me signed in',
                                style: TextStyle(color: AppColors.textMuted, fontSize: 14),
                              ),
                            ],
                          ),
                        ),
                        const SizedBox(height: 16),
                        ElevatedButton(
                          onPressed: _isLoading ? null : _login,
                          child: _isLoading
                              ? const SizedBox(
                                  height: 20,
                                  width: 20,
                                  child: CircularProgressIndicator(strokeWidth: 2, color: AppColors.text),
                                )
                              : const Text('Sign In', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                        ),
                        const SizedBox(height: 12),
                        TextButton(
                          onPressed: () {
                            _forgotUsernameController.text = _usernameController.text.trim();
                            setState(() {
                              _showForgotPassword = true;
                              _error = null;
                            });
                          },
                          child: const Text('Forgot Password?', style: TextStyle(color: AppColors.textMuted)),
                        ),
                      ],
                    ),
                  )
                else
                  Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      const Text(
                        'Enter your username and a password reset request will be sent to the admin for approval.',
                        style: TextStyle(color: AppColors.textMuted, fontSize: 13),
                      ),
                      const SizedBox(height: 14),
                      TextField(
                        controller: _forgotUsernameController,
                        decoration: const InputDecoration(
                          labelText: 'Username',
                          prefixIcon: Icon(LucideIcons.user),
                        ),
                        onSubmitted: (_) => _requestPasswordReset(),
                      ),
                      const SizedBox(height: 16),
                      ElevatedButton(
                        onPressed: _isLoading ? null : _requestPasswordReset,
                        child: _isLoading
                            ? const SizedBox(
                                height: 20,
                                width: 20,
                                child: CircularProgressIndicator(strokeWidth: 2, color: AppColors.text),
                              )
                            : const Text('Request Reset'),
                      ),
                      const SizedBox(height: 10),
                      TextButton(
                        onPressed: () => setState(() {
                          _showForgotPassword = false;
                          _error = null;
                        }),
                        child: const Text('Back to Login', style: TextStyle(color: AppColors.textMuted)),
                      ),
                    ],
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
