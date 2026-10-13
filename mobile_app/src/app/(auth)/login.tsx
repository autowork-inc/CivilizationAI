import { Ionicons } from '@expo/vector-icons';
import { Link } from 'expo-router';
import { useRef, useState } from 'react';
import { Alert, Pressable, Text, TextInput, View } from 'react-native';

import { getErrorMessage } from '@/api/apiClient';
import { AuthHeader } from '@/components/AuthHeader';
import { Button } from '@/components/Button';
import { GoogleButton } from '@/components/GoogleButton';
import { Screen } from '@/components/Screen';
import { TextField } from '@/components/TextField';
import { useAuth } from '@/context/AuthContext';

export default function LoginScreen() {
  const { login } = useAuth();
  const passwordRef = useRef<TextInput>(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!email.trim() || !password) {
      setError('Введите почту и пароль.');
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await login(email, password); // on success the root layout swaps (auth) for (app)
    } catch (e) {
      setError(getErrorMessage(e));
      setBusy(false);
    }
  }

  return (
    <Screen>
      <AuthHeader title="С возвращением" subtitle="Войдите, чтобы продолжить разговор со своим ИИ-аватаром." />

      <TextField
        label="Email"
        value={email}
        onChangeText={setEmail}
        placeholder="you@example.com"
        keyboardType="email-address"
        textContentType="emailAddress"
        autoComplete="email"
        returnKeyType="next"
        blurOnSubmit={false}
        onSubmitEditing={() => passwordRef.current?.focus()}
      />
      <TextField
        ref={passwordRef}
        label="Пароль"
        value={password}
        onChangeText={setPassword}
        placeholder="••••••••"
        secureTextEntry
        textContentType="password"
        autoComplete="password"
        returnKeyType="go"
        onSubmitEditing={submit}
      />

      {error ? (
        <View className="mb-4 flex-row items-center rounded-2xl border border-red-500/30 bg-red-500/10 px-4 py-3">
          <Ionicons name="alert-circle" size={18} color="#f87171" />
          <Text className="ml-2 flex-1 text-sm text-red-300">{error}</Text>
        </View>
      ) : null}

      <View className="mt-2">
        <Button title="Войти" onPress={submit} loading={busy} />
      </View>

      <View className="my-6 flex-row items-center">
        <View className="h-px flex-1 bg-slate-800" />
        <Text className="mx-4 text-sm text-slate-500">или</Text>
        <View className="h-px flex-1 bg-slate-800" />
      </View>

      {/* Placeholder: real Google sign-in needs native modules (expo-auth-session) and an OAuth client. */}
      <GoogleButton onPress={() => Alert.alert('Скоро', 'Авторизация через Google находится в разработке')} />

      <Link href="/register" asChild>
        <Pressable className="mt-6 items-center py-2" hitSlop={8}>
          <Text className="text-base text-slate-400">
            Нет аккаунта? <Text className="font-semibold text-blue-400">Зарегистрироваться</Text>
          </Text>
        </Pressable>
      </Link>
    </Screen>
  );
}
