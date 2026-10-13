import { useRef, useState } from 'react';
import { ActivityIndicator, FlatList, KeyboardAvoidingView, Platform, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ChatHeader } from '@/components/chat/ChatHeader';
import { ChatInput } from '@/components/chat/ChatInput';
import { ErrorBanner } from '@/components/chat/ErrorBanner';
import { MessageBubble } from '@/components/chat/MessageBubble';
import { TypingIndicator } from '@/components/chat/TypingIndicator';
import type { ChatMessage } from '@/components/chat/types';
import { useAuth } from '@/context/AuthContext';
import { useChat } from '@/hooks/useChat';

export default function ChatScreen() {
  const { logout } = useAuth();
  const chat = useChat();
  const listRef = useRef<FlatList<ChatMessage>>(null);
  const [loggingOut, setLoggingOut] = useState(false);

  async function handleLogout() {
    setLoggingOut(true);
    await logout(); // the root layout swaps (app) for (auth)
  }

  return (
    <SafeAreaView edges={['top']} className="flex-1 bg-slate-950">
      <ChatHeader title={chat.title} subtitle="Civilization AI" onLogout={handleLogout} loggingOut={loggingOut} />

      <KeyboardAvoidingView className="flex-1" behavior={Platform.OS === 'ios' ? 'padding' : 'height'}>
        {chat.loading ? (
          <View className="flex-1 items-center justify-center">
            <ActivityIndicator color="#60a5fa" />
          </View>
        ) : (
          <FlatList
            ref={listRef}
            data={chat.messages}
            keyExtractor={(m) => m.id}
            renderItem={({ item }) => <MessageBubble message={item} />}
            ListEmptyComponent={
              <View className="items-center px-8 pt-24">
                <Text className="text-center text-base leading-6 text-slate-500">Здесь пока пусто. Напишите первое сообщение.</Text>
              </View>
            }
            ListFooterComponent={chat.typing ? <TypingIndicator /> : null}
            contentContainerClassName="px-4 pb-2 pt-4"
            keyboardShouldPersistTaps="handled"
            keyboardDismissMode={Platform.OS === 'ios' ? 'interactive' : 'on-drag'}
            showsVerticalScrollIndicator={false}
            onContentSizeChange={() => listRef.current?.scrollToEnd({ animated: true })}
          />
        )}

        {chat.error ? (
          <ErrorBanner message={chat.error.message} onRetry={chat.retry} onDismiss={chat.dismissError} />
        ) : chat.loadError ? (
          <ErrorBanner message={chat.loadError} onRetry={chat.reload} />
        ) : null}

        <ChatInput onSend={chat.send} sendDisabled={chat.sending || chat.loading} />
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}
